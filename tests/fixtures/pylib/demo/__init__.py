"""Demo package."""


def greet(name, times=1):
    """Say hello."""
    return name


class Box:
    """Holds a value."""

    def put(self, value):
        """Store it."""
        self.value = value

    @property
    def label(self):
        """The label."""
        return ""

    @classmethod
    def born(cls):
        """A new box."""
        return cls()


_box = Box()
put = _box.put
