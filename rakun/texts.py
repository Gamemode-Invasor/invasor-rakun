"""What Rakun shows in Invasor's panel and in Steam's notifications, in one place.

The core notifies a task twice with the same title (its label): at the start with no body and at the end with its
final text as the body. So a label says what is being done, and the final text says how it ended. A quick
notification (a game uninstalled or imported) has the same shape: a title naming the game, the result as the body."""

PREFIX = "Rakun"
MAX_LABEL = 64  # what the core keeps of a task's label (and of a notification's title)
MAX_TEXT = 256  # and of the text of a task or the body of a notification

# Rakun's status of an operation on a game -> (what is being done, how it ended)
OPERATIONS = {
    "installing": ("Installing", "Installed"),
    "updating": ("Updating", "Updated"),
    "repairing": ("Repairing", "Repaired"),
    "moving": ("Moving", "Moved"),
    "extracting": ("Extracting", "Extracted"),
}
DEFAULT_OPERATION = OPERATIONS["installing"]
STARTING = "Starting"

# Rakun's status of an operation that did not end well -> how it is told
FAILED = {"error": "Failed", "failed": "Failed", "canceled": "Cancelled", "cancelled": "Cancelled", "aborted": "Cancelled"}
FAILED_DEFAULT = "Failed"
PAUSED = "Paused"
RAKUN_STOPPED = "Rakun stopped"
NO_NEWS = "No news from Rakun"
MODULE_UNLOADED = "Module unloaded"

# Quick operations: only a notification, with the game's title as its title and this as its body
UNINSTALLED = "Uninstalled"
IMPORTED = "Imported"

# Installing and updating Rakun itself
INSTALLING_RAKUN = "Installing Rakun"
UPDATING_RAKUN = "Updating Rakun"
LOOKING_FOR_RELEASE = "Looking for the latest release"
DOWNLOADING_INSTALLER = "Downloading the installer"
CHECKING_INSTALLATION = "Checking the installation"
RUNNING_SELF_UPDATE = "Running self-update"
CHECKING_VERSION = "Checking the new version"


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def title_of(game):
    """The title of a notification about a game: "Rakun: <game>"."""
    return _clip("%s: %s" % (PREFIX, game), MAX_LABEL)


def label_of(verb, game):
    """The label of a game's task: "Rakun: <doing> <game>"; the game's title is what gives way when it is long."""
    head = "%s: %s " % (PREFIX, verb)
    return head + _clip(game, MAX_LABEL - len(head))


def text_of(text):
    return _clip(text, MAX_TEXT)


def installing(version):
    return "Installing %s" % version


def installed(version=""):
    return "Installed %s" % version if version else "Installed"


def updated(version=""):
    return "Updated to %s" % version if version else "Updated"
