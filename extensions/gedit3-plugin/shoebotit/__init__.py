from distutils.spawn import find_executable as which
from urllib.request import pathname2url

import queue
import os
import subprocess

from gi.repository import Gtk, Gio, GObject, Gedit, Pango
from gettext import gettext as _
from shoebotit import ide_utils, gtk3_utils

if not which('sbot'):
    print('Shoebot executable not found.')


class ShoebotProcess(object):
    def __init__(self, code, use_socketserver, show_varwindow, use_fullscreen, title, cwd=None):
        command = ['sbot', '-w', '-t%s' % title]

        if use_socketserver:
            command.append('-s')

        if not show_varwindow:
            command.append('-dv')

        if use_fullscreen:
            command.append('-f')

        command.append(code)

        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1, close_fds=True, shell=False, cwd=cwd)

        # Launch the asynchronous readers of the process' stdout and stderr.
        self.stdout_queue = queue.Queue()
        self.stdout_reader = ide_utils.AsynchronousFileReader(self.process.stdout, self.stdout_queue)
        self.stdout_reader.start()
        self.stderr_queue = queue.Queue()
        self.stderr_reader = ide_utils.AsynchronousFileReader(self.process.stderr, self.stderr_queue)
        self.stderr_reader.start()

    def _update_ui_text(self, output_widget):
        textbuffer = output_widget.get_buffer()
        while not (self.stdout_queue.empty() and self.stderr_queue.empty()):
            if not self.stdout_queue.empty():
                line = self.stdout_queue.get().decode('utf-8')
                textbuffer.insert(textbuffer.get_end_iter(), line)

            if not self.stderr_queue.empty():
                line = self.stderr_queue.get().decode('utf-8')
                textbuffer.insert(textbuffer.get_end_iter(), line)
        #else:
        output_widget.scroll_to_iter(textbuffer.get_end_iter(), 0.0, True, 0.0, 0.0)
        while Gtk.events_pending():
            Gtk.main_iteration()

    def update_shoebot(self, output_widget):
        """
        :param textbuffer: Gtk textbuffer to append shoebot output to.
        :output_widget: Gtk widget containing the output
        :return: True if shoebot still running, False if terminated.
        Send any new shoebot output to a textbuffer.
        """
        self._update_ui_text(output_widget)

        if self.process.poll() is not None:
            # Close subprocess' file descriptors.
            self._update_ui_text(output_widget)
            self.process.stdout.close()
            self.process.stderr.close()
            return False

        result = not (self.stdout_reader.eof() or self.stderr_reader.eof())
        return result


class ShoebotWindowHelper(object):
    def __init__(self, plugin, window):
        self.example_bots = {}

        self.window = window
        panel = window.get_bottom_panel()
        self.output_widget = gtk3_utils.get_child_by_name(panel, 'shoebot-output')

        self.plugin = plugin
        self.insert_menu()
        self.shoebot_window = None
        self.id_name = 'ShoebotPluginID'

        self.use_socketserver = False
        self.show_varwindow = True
        self.use_fullscreen = False

        self.started = False
        self.shoebot_thread = None
        
        for view in self.window.get_views():
            self.connect_view(view)

        self.bot = None
    
    def deactivate(self):
        self.remove_menu()
        self.window = None
        self.plugin = None
        self.action_group = None
        del self.shoebot_window

    def insert_menu(self):
        examples_xml, example_actions, submenu_actions = gtk3_utils.examples_menu()
        ui_str = gtk3_utils.gedit3_menu(examples_xml)

        manager = self.window.get_ui_manager()
        self.action_group = Gtk.ActionGroup("ShoebotPluginActions")
        self.action_group.add_actions([
            ("Shoebot", None, _("Shoe_bot"), None, _("Shoebot"), None),
            ("ShoebotRun", None, _("Run in Shoebot"), '<control>R', _("Run in Shoebot"), self.on_run_activate),
            ('ShoebotOpenExampleMenu', None, _('E_xamples'), None, None, None)
            ])
        

        for action, label in example_actions:
            self.action_group.add_actions([(action, None, (label), None, None, self.on_open_example)])

        for action, label in submenu_actions:
            self.action_group.add_actions([(action, None, (label), None, None, None)])

        self.action_group.add_toggle_actions([
            ("ShoebotSocket", None, _("Enable Socket Server"), '<control><alt>S', _("Enable Socket Server"), self.toggle_socket_server, False),
            ("ShoebotVarWindow", None, _("Show Variables Window"), '<control><alt>V', _("Show Variables Window"), self.toggle_var_window, False),
            ("ShoebotFullscreen", None, _("Go Fullscreen"), '<control><alt>F', _("Go Fullscreen"), self.toggle_fullscreen, False),
            ])
        manager.insert_action_group(self.action_group)
        
        self.ui_id = manager.add_ui_from_string(ui_str)
        manager.ensure_update()

    def on_open_example(self, action):
        example_dir = ide_utils.get_example_dir()
        filename = os.path.join(example_dir, action.get_name()[len('ShoebotOpenExample'):].strip())
        
        uri      = "file:///" + pathname2url(filename)
        gio_file = Gio.file_new_for_uri(uri)
        self.window.create_tab_from_location(
            gio_file,
            None,  # encoding
            0,
            0,     # column
            False, # Do not create an empty file
            True)  # Switch to the tab

    def remove_menu(self):
        manager = self.window.get_ui_manager()
        manager.remove_action_group(self.action_group)
        for bot, ui_id in self.example_bots.items():
            manager.remove_ui(ui_id)
        manager.remove_ui(self.ui_id)

        # Make sure the manager updates
        manager.ensure_update()

    def update_ui(self):
        self.action_group.set_sensitive(self.window.get_active_document() != None)
        # hack to make sure that views are connected
        # since activate() is not called on startup
        if not self.started and self.window.get_views():
            for view in self.window.get_views():
                self.connect_view(view)
            self.started = True

    def on_run_activate(self, action):
        self.start_shoebot()

    def start_shoebot(self):
        if not which('sbot'):
            textbuffer = self.output_widget.get_buffer()
            textbuffer.set_text('Cannot find sbot in path.')
            while Gtk.events_pending():
               Gtk.main_iteration()
            return False
            
        if self.bot and self.bot.process.poll() == None:
            print('Has a bot already')
            return False
        
        # get the text buffer
        doc = self.window.get_active_document()
        if not doc:
            return

        title = '%s - Shoebot on gedit' % doc.get_short_name_for_display()
        cwd = os.path.dirname(doc.get_uri_for_display()) or None

        start, end = doc.get_bounds()
        code = doc.get_text(start, end, False)
        if not code:
            return False

        textbuffer = self.output_widget.get_buffer()
        textbuffer.set_text('')
        while Gtk.events_pending():
           Gtk.main_iteration()


        self.bot = ShoebotProcess(code, self.use_socketserver, self.show_varwindow, self.use_fullscreen, title, cwd=cwd)

        GObject.idle_add(self.update_shoebot)


    def update_shoebot(self):
        if self.bot:
            running = self.bot.update_shoebot(self.output_widget)

            if not running:
                 return False
        return True

    
    def toggle_socket_server(self, action):
        self.use_socketserver = action.get_active()
    def toggle_var_window(self, action):
        self.show_varwindow = action.get_active()
    def toggle_fullscreen(self, action):
        #no full screen for you!
        #self.use_fullscreen = False
        self.use_fullscreen = action.get_active()


    def connect_view(self, view):
        # taken from gedit-plugins-python-openuricontextmenu
        #handler_id = view.connect('populate-popup', self.on_view_populate_popup)
        #view.set_data(self.id_name, [handler_id])

        pass


class ShoebotPlugin(GObject.Object, Gedit.WindowActivatable):
    window = GObject.property(type=Gedit.Window)

    def __init__(self):
        GObject.Object.__init__(self)
        self.instances = {}
        self.tempfiles = []

    def do_activate(self):
        self.text = Gtk.TextView()
        self.text.set_editable(False)
        fontdesc = Pango.FontDescription("Monospace")
        self.text.modify_font(fontdesc)
        self.text.set_name('shoebot-output')
        self.panel = self.window.get_bottom_panel()

        image = Gtk.Image()
        image.set_from_stock(Gtk.STOCK_EXECUTE, Gtk.IconSize.BUTTON)

        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.add(self.text)
        scrolled_window.show_all()
        
        self.panel.add_item(scrolled_window, 'Shoebot', 'Shoebot', image)   
        self.output_widget = scrolled_window

        self.instances[self.window] = ShoebotWindowHelper(self, self.window)


    def do_deactivate(self):
        self.panel.remove_item(self.text)
        self.instances[self.window].deactivate()
        del self.instances[self.window]
        for tfilename in self.tempfiles:
            os.remove(tfilename)

        self.panel.remove_item(self.output_widget)


    def do_update_state(self):
        self.instances[self.window].update_ui()


