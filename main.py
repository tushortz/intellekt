import sublime
import sublime_plugin
from .language import helpers, _java


class JavaMethodCompletion(sublime_plugin.ViewEventListener):
    def on_query_completions(self, prefix, locations):
        scope = self.view.scope_name(0)

        if not "source.java" in scope or len(prefix) < 2:
            return

        return _java.get_methods("java.io", "*")


        
