import sublime
import sublime_plugin
from .language import helpers, _java


class JavaMethodCompletion(sublime_plugin.ViewEventListener):
    def on_query_completions(self, prefix, locations):
        scope = self.view.scope_name(0)

        if not "source.java" in scope or len(prefix) < 2:
            return

        region = sublime.Region(0, self.view.size())
        view_text = self.view.substr(region)
        imports = _java.get_imports_from_view(view_text)
        
        methods = []
        for im in imports:
            if len(im) == 2:
                java_methods = _java.get_methods(im)
                print(java_methods)
                methods.extend(java_methods)

        methods = sorted(set(methods))
        print(methods)
        return methods


        
