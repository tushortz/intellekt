import sublime
import sublime_plugin
from .helpers import java


class JavaMethodCompletion(sublime_plugin.ViewEventListener):
    def on_query_completions(self, prefix, locations):


        return [
            ["♦ definition\tsupress", "def ${1:name}($2) { $0 }"],
            ["for\why", "for ($1; $2; $3) { $0 }"]
        ]

        
