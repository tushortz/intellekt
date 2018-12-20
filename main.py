import sublime
import sublime_plugin
import webbrowser

from .language import _java, helpers, _python


class JavaCompletion(sublime_plugin.ViewEventListener):
    def on_query_completions(self, prefix, locations):
        scope = self.view.scope_name(0)

        if not "source.java" in scope or len(prefix) < 2:
            return

        region = sublime.Region(0, self.view.size())
        view_text = self.view.substr(region)
        imports = _java.get_imports_from_view(view_text)
        result = []

        for im in imports:
            if len(im) == 2:
                package, class_name = im
                java_methods = _java.get_methods(package, class_name)
                result += java_methods

        result = sorted(result)
        result = (result, sublime.INHIBIT_WORD_COMPLETIONS |
                  sublime.INHIBIT_EXPLICIT_COMPLETIONS)
        return result

    def on_modified(self):
        scope = self.view.scope_name(0)
        current_line = self.view.substr(self.view.line(self.view.sel()[0]))

        if not "source.java" in scope or len(current_line) < 2:
            return


        if "import " in current_line and len(current_line) > 7:
            suggestions = _java.suggest_import(current_line)
            self.view.show_popup(
                suggestions, 0, sublime.HIDE_ON_MOUSE_MOVE_AWAY,
                on_navigate=lambda x: webbrowser.open(x))

    def on_hover(self, point, hover_zone):
        scope = self.view.scope_name(0)
        current_line = self.view.substr(self.view.line(self.view.sel()[0]))

        if not "source.java" in scope or len(current_line) < 2:
            return


        if "import " in current_line and len(current_line) > 7:
            self.view.window().status_message("Searching for doc ...")

            try:
                doc = _java.get_documentation(current_line)
                self.view.show_popup(doc, 0, sublime.HIDE_ON_MOUSE_MOVE_AWAY,
                                     on_navigate=lambda x: print(x))
            except Exception as err:
                self.view.window().status_message("Error: " + str(err))


class PythonCompletion(sublime_plugin.ViewEventListener):
    def on_query_completions(self, prefix, locations):
        scope = self.view.scope_name(0)

        if not "source.python" in scope or len(prefix) < 2:
            return

        region = sublime.Region(0, self.view.size())
        view_text = self.view.substr(region)
        imports = _python.get_imports_from_view(view_text)
        result = []

        for im in imports:
            if len(im) == 2:
                package, class_name = im
                python_methods = _python.get_module_members(package, class_name)
                result += python_methods

        result = sorted(result, key=lambda x: x[0].lower())
        result = (result, sublime.INHIBIT_WORD_COMPLETIONS |
                  sublime.INHIBIT_EXPLICIT_COMPLETIONS)
        return result