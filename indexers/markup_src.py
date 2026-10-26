"""CSS properties and HTML tags.

Neither language has a toolchain to scan. The names are the language.
Values and attributes are the small closed sets. A property such as
``width`` has no value list.
"""


_PROPERTIES = (
    "accent-color", "align-content", "align-items", "align-self", "all",
    "animation", "appearance", "aspect-ratio", "backdrop-filter", "background",
    "background-color", "background-image", "background-position",
    "background-repeat", "background-size", "border", "border-collapse",
    "border-color", "border-radius", "border-spacing", "border-style",
    "border-width", "bottom", "box-shadow", "box-sizing", "clear", "clip-path",
    "color", "color-scheme", "column-gap", "columns", "content", "cursor",
    "direction", "display", "filter", "flex", "flex-basis", "flex-direction",
    "flex-flow", "flex-grow", "flex-shrink", "flex-wrap", "float", "font",
    "font-family", "font-size", "font-style", "font-weight", "gap", "grid",
    "grid-area", "grid-column", "grid-row", "grid-template",
    "grid-template-columns", "grid-template-rows", "height", "inset",
    "isolation", "justify-content", "justify-items", "justify-self", "left",
    "letter-spacing", "line-height", "list-style", "list-style-type", "margin",
    "max-height", "max-width", "min-height", "min-width", "object-fit",
    "opacity", "order", "outline", "overflow", "overflow-x", "overflow-y",
    "overscroll-behavior", "padding", "place-items", "pointer-events",
    "position", "resize", "right", "row-gap", "scroll-behavior", "text-align",
    "text-decoration", "text-indent", "text-overflow", "text-shadow",
    "text-transform", "top", "transform", "transition", "user-select",
    "vertical-align", "visibility", "white-space", "width", "word-break",
    "word-spacing", "z-index",
)

_VALUES = {
    "align-items": ("stretch", "flex-start", "flex-end", "center", "baseline", "start", "end"),
    "appearance": ("none", "auto"),
    "border-style": ("none", "solid", "dashed", "dotted", "double"),
    "box-sizing": ("content-box", "border-box"),
    "clear": ("none", "left", "right", "both"),
    "cursor": ("auto", "default", "pointer", "text", "move", "not-allowed", "grab", "wait"),
    "display": (
        "block", "inline", "inline-block", "flex", "inline-flex", "grid",
        "inline-grid", "none", "contents", "flow-root",
    ),
    "flex-direction": ("row", "row-reverse", "column", "column-reverse"),
    "flex-wrap": ("nowrap", "wrap", "wrap-reverse"),
    "float": ("none", "left", "right"),
    "font-style": ("normal", "italic", "oblique"),
    "font-weight": ("normal", "bold", "lighter", "bolder"),
    "justify-content": (
        "flex-start", "flex-end", "center", "space-between", "space-around",
        "space-evenly", "start", "end",
    ),
    "list-style-type": ("disc", "circle", "square", "none", "decimal"),
    "object-fit": ("fill", "contain", "cover", "none", "scale-down"),
    "overflow": ("visible", "hidden", "scroll", "auto", "clip"),
    "overflow-x": ("visible", "hidden", "scroll", "auto", "clip"),
    "overflow-y": ("visible", "hidden", "scroll", "auto", "clip"),
    "pointer-events": ("auto", "none"),
    "position": ("static", "relative", "absolute", "fixed", "sticky"),
    "text-align": ("left", "right", "center", "justify", "start", "end"),
    "text-decoration": ("none", "underline", "overline", "line-through"),
    "text-transform": ("none", "uppercase", "lowercase", "capitalize"),
    "user-select": ("auto", "none", "text", "all"),
    "vertical-align": ("baseline", "top", "middle", "bottom", "sub", "super"),
    "visibility": ("visible", "hidden", "collapse"),
    "white-space": ("normal", "nowrap", "pre", "pre-wrap", "pre-line"),
}

_TAGS = (
    "a", "abbr", "address", "article", "aside", "audio", "b", "blockquote",
    "body", "br", "button", "canvas", "caption", "cite", "code", "col",
    "colgroup", "data", "datalist", "dd", "del", "details", "dfn", "dialog",
    "div", "dl", "dt", "em", "embed", "fieldset", "figcaption", "figure",
    "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header",
    "hr", "html", "i", "iframe", "img", "input", "label", "legend", "li",
    "link", "main", "map", "mark", "meta", "nav", "noscript", "object", "ol",
    "optgroup", "option", "output", "p", "picture", "pre", "progress", "q",
    "rp", "rt", "ruby", "s", "samp", "script", "section", "select", "small",
    "source", "span", "strong", "style", "sub", "summary", "sup", "table",
    "tbody", "td", "template", "textarea", "tfoot", "th", "thead", "time",
    "title", "tr", "track", "u", "ul", "var", "video", "wbr",
)

_GLOBAL = (
    "accesskey", "class", "contenteditable", "dir", "draggable", "hidden",
    "id", "lang", "role", "slot", "spellcheck", "style", "tabindex", "title",
    "translate",
)

_ATTRS = {
    "a": ("href", "target", "rel", "download", "hreflang"),
    "audio": ("src", "controls", "autoplay", "loop", "muted"),
    "button": ("type", "name", "value", "disabled"),
    "form": ("action", "method", "enctype", "novalidate"),
    "iframe": ("src", "width", "height", "title", "allow", "sandbox"),
    "img": ("src", "alt", "width", "height", "srcset", "sizes"),
    "input": (
        "type", "name", "value", "placeholder", "checked", "disabled",
        "required", "readonly", "maxlength", "min", "max", "step", "accept",
    ),
    "label": ("for",),
    "link": ("href", "rel", "type", "media"),
    "meta": ("charset", "name", "content", "http-equiv"),
    "ol": ("start", "reversed", "type"),
    "option": ("value", "selected", "label"),
    "script": ("src", "type", "async", "defer"),
    "select": ("name", "multiple", "required", "disabled"),
    "source": ("src", "type", "srcset"),
    "td": ("colspan", "rowspan"),
    "textarea": ("name", "rows", "cols", "placeholder", "disabled", "required"),
    "th": ("colspan", "rowspan"),
    "video": ("src", "controls", "autoplay", "loop", "muted", "poster", "width", "height"),
}


def _symbol(owner, name, kind):
    return {
        "t": "s", "owner": owner, "name": name, "kind": kind,
        "signature": name, "doc": "",
    }


def css_records():
    for name in _PROPERTIES:
        yield _symbol("css", name, "variable")
    for prop, values in _VALUES.items():
        for name in values:
            yield _symbol(prop, name, "variable")


def html_records():
    for name in _TAGS:
        yield _symbol("html", name, "type")
    for name in _GLOBAL:
        yield _symbol("global", name, "variable")
    for tag, names in _ATTRS.items():
        for name in names:
            yield _symbol(tag, name, "variable")
