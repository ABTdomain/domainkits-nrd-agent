"""Shared code of scripts/pull.py and scripts/run.py (SKILL.md section 3): times, keys, the lock, the task files
and the archive. Times are UTC, written as YYYY-MM-DDTHH:MMZ."""
import csv
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

COLUMNS = ["domain", "last-check", "status", "action", "action-date"]
TIME = "%Y-%m-%dT%H:%MZ"


# ---------------------------------------------------------------- times
def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fmt(t):
    return t.strftime(TIME)


def parse(text):
    return datetime.strptime(text, TIME)


def duration(text):
    """'<n>h' or '<n>d' as a timedelta. Raises ValueError for anything else, such as "30m" or "3 days"."""
    m = re.fullmatch(r"([0-9]{1,5})([hd])", str(text))
    if not m:
        raise ValueError(f"{text!r} is no duration: write a whole number with h for hours or d for days, as \"6h\" "
                         "or \"3d\"")
    n = int(m.group(1))
    return timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)


# ---------------------------------------------------------------- keys and lock
def key(task, config, role):
    """The value of the key named by config["keys"][role]: from the environment, else from the file named by
    env_file (KEY=VALUE lines; a relative path is read from the task folder). The value goes to a client only."""
    name = config["keys"][role]
    if os.environ.get(name, "").strip():
        return os.environ[name].strip()
    path = config.get("env_file")
    if path and os.path.isfile(os.path.join(task, path)):
        with open(os.path.join(task, path), encoding="utf-8") as f:
            for line in f:
                k, sep, value = line.strip().partition("=")
                if sep and k.replace("export ", "").strip() == name:
                    return value.strip().strip("'\"")
    return ""


def unset(task, config, names):
    """The message for keys that could not be read: their names and where they were looked for. A value under keys
    that is no name of a variable, as a key written there by mistake, is not printed."""
    names = [n if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", str(n)) else
             "(a value under keys that is no name of a variable: write the name of the variable that holds the key, "
             "not the key)" for n in names]
    path = config.get("env_file")
    if not path:
        where = "the environment, and config.json names no env_file (a field at its top level, not inside keys)"
    elif not os.path.isfile(os.path.join(task, path)):
        where = f"the environment, and the env_file {path} does not exist"
    else:
        where = f"the environment or in {path}"
    return f"Not set in {where}: {', '.join(names)}"


LOCK_STALE = timedelta(hours=2)
_held = {}     # task folder -> the token this process wrote into its run.lock


class LockLost(Exception):
    """run.lock is no longer this run's. The run stops and writes nothing more."""


def _token(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def held(task):
    """Raises LockLost where this process took the lock of the task and run.lock no longer holds its token: another
    start took the lock over, or the file was removed. Called at each touch of the lock and before each write of a
    task file, so that a run that was taken for stopped, as after a sleep of the machine, does not write over the
    work of the run that took its place."""
    token = _held.get(task)
    if token is not None and _token(os.path.join(task, "run.lock")) != token:
        raise LockLost(f"{os.path.join(task, 'run.lock')} is no longer this run's: another start took it over, or it "
                       f"was removed. A lock is taken over when it was not touched for "
                       f"{int(LOCK_STALE.total_seconds() // 3600)} hours, as after a sleep of the machine. This run "
                       "writes nothing more.")


def lock(task):
    """Creates run.lock in the task folder, with a token of this run in it; False, after a message, when a run is in
    progress. A program that runs touches the file as it works (beat()). A lock that was not touched for LOCK_STALE
    is left over from a run that died, such as at a restart of the machine: this start takes it over by writing its
    own token into it, and the log says so."""
    path = os.path.join(task, "run.lock")
    token = secrets.token_hex(8)
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(token)
    except FileExistsError:
        try:
            idle = timedelta(seconds=time.time() - os.path.getmtime(path))
        except OSError:
            idle = timedelta(0)
        if idle < LOCK_STALE:
            print(f"{path} exists: a run of this task is in progress. A lock that is not touched for "
                  f"{int(LOCK_STALE.total_seconds() // 3600)} hours is taken over by the next start.")
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write(token)
        _held[task] = token
        log(task, "lock", "", "taken over", f"run.lock was not touched for {int(idle.total_seconds() // 3600)} "
                                            "hours: left over from a run that stopped")
    _held[task] = token
    return True


def beat(task):
    """Touches run.lock: the sign that the run that holds it is still at work. Raises LockLost where the lock is no
    longer this run's (held())."""
    held(task)
    try:
        os.utime(os.path.join(task, "run.lock"))
    except OSError:
        pass


def unlock(task):
    """Removes run.lock where it is still this run's. A lock that another start took over is that run's: it stays."""
    path = os.path.join(task, "run.lock")
    token = _held.pop(task, None)
    if token is None or _token(path) == token:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------- task files
def not_set_up(task):
    """The message for a folder without config.json, "" where it holds one. A task begins in a conversation with the
    AI, which writes that file (SKILL.md section 1): a program that is started first has nothing to work from."""
    if os.path.isfile(os.path.join(task, "config.json")):
        return ""
    skill = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "SKILL.md")
    return (f"No config.json in {task}: this task is not set up yet. Set it up first in a conversation with your AI "
            f"assistant, which reads {skill} (to watch a brand: SKILL.md in the folder protection beside it). In "
            "that conversation you settle what to look for, the filters, how many days back the pulls look, and how "
            "often new domains are pulled and each site is checked. It writes config.json into the task folder. "
            "Then start this program again.")


def load_config(task):
    with open(os.path.join(task, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def config_problems(config, program):
    """What config.json lacks, or holds in a form the program cannot work with, as one message each; [] where the
    program can run on it. Checked at the start, before any request, so that a slip in the file costs nothing and
    does not stop a run half way. program: "pull" or "run"."""
    out = []

    def need(ok, text):
        if not ok:
            out.append(text)

    def whole(value):
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    def steps(where, value, ladder_ok=True):
        if value == "ladder" and ladder_ok:
            return
        try:
            duration(value)
        except ValueError as e:
            out.append(f"{where}: {e}" + (", or \"ladder\"" if ladder_ok else ""))

    exclude = config.get("exclude")
    need(isinstance(exclude, list) and all(isinstance(d, str) for d in exclude),
         "exclude is a list of domain names ([] for none)")
    need(isinstance(config.get("keys"), dict), "keys names the variables that hold the keys (SKILL.md 1.7)")
    if program == "pull":
        pulls = config.get("pulls")
        need(isinstance(pulls, list) and pulls and all(isinstance(p, dict) and isinstance(p.get("kind"), str)
                                                       for p in pulls),
             "pulls is a list of pulls, each with its kind (SKILL.md 1.7)")
        need(whole(config.get("window_days", 3)), "window_days is a whole number of days above 0")
        if config.get("pull_every") is not None:
            steps("pull_every", config["pull_every"], ladder_ok=False)
        return out
    for field in ("intent", "related", "purpose"):
        need(isinstance(config.get(field), str), f"{field} is a text (SKILL.md 1.7)")
    models = config.get("models")
    need(isinstance(models, dict) and isinstance(models.get("judge"), str) and models["judge"].strip(),
         "models names the model that judges, as {\"judge\": \"claude-sonnet-5-5\"}")
    if isinstance(models, dict) and models.get("change"):
        need(isinstance(models["change"], str), "models.change is the name of a model")
    limits = config.get("limits")
    need(whole(limits.get("checks_per_run") if isinstance(limits, dict) else None),
         "limits.checks_per_run is a whole number above 0")
    steps("min_interval", config.get("min_interval"), ladder_ok=False)
    frequency = config.get("frequency")
    if not isinstance(frequency, dict):
        out.append("frequency holds the step of each state (SKILL.md 1.7)")
        return out
    for state, value in frequency.items():
        if state == "ladder":
            need(isinstance(value, dict), "frequency.ladder holds the rungs, as {\"3d\": \"3h\", \"7d\": \"6h\"}")
            for bound, step in (value.items() if isinstance(value, dict) else ()):
                steps(f"frequency.ladder {bound}", bound, ladder_ok=False)
                steps(f"frequency.ladder {bound}", step, ladder_ok=False)
        elif isinstance(value, dict):
            for tier, step in value.items():
                steps(f"frequency.{state}.{tier}", step)
        else:
            steps(f"frequency.{state}", value, ladder_ok=state != "robots_disallow")
    return out


def save_config(task, config):
    """config.json written again in full: each field kept, in its order."""
    held(task)
    path = os.path.join(task, "config.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(path + ".tmp", path)


def read_state(task):
    path = os.path.join(task, "state.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def cell(text):
    """A text as a cell of a CSV file that a spreadsheet program opens: one that begins with =, +, -, @, a tab or a
    carriage return would be run there as a formula, so it gets an apostrophe in front."""
    text = str(text)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def write_state(task, rows):
    """state.csv replaced in full: a temp file first, then os.replace, so a crash keeps the last full file. Windows
    refuses the replacement while another program holds state.csv, as a scanner does for a moment: it is tried once
    more after half a second."""
    held(task)
    path = os.path.join(task, "state.csv")
    with open(path + ".tmp", "w", encoding="utf-8", newline="") as f:
        out = csv.DictWriter(f, fieldnames=COLUMNS)
        out.writeheader()
        out.writerows({**row, "action": cell(row["action"])} for row in rows)
    try:
        os.replace(path + ".tmp", path)
    except PermissionError:
        time.sleep(0.5)
        os.replace(path + ".tmp", path)


def append_line(path, obj):
    """One JSON line appended. A text that cannot be written as UTF-8 (a lone surrogate in a page) is written with
    escapes; a last line that a stop cut short gets its line end first, so the new line stands on its own."""
    line = json.dumps(obj, ensure_ascii=False)
    try:
        line.encode("utf-8")
    except UnicodeEncodeError:
        line = json.dumps(obj)
    with open(path, "a+b") as f:
        f.seek(0, os.SEEK_END)
        if f.tell():
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                f.write(b"\n")
        f.write((line + "\n").encode("utf-8"))


DOMAIN = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Names Windows gives to devices: a file named after one of them, with any ending, is no file there.
DEVICES = {"con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(10)}


def is_domain(text):
    """True for a domain name as the programs take it: lower case letters, digits and hyphens in labels joined by
    dots (an international name in its xn-- form). Anything else, such as a path or a name with an @, is no name of
    a domain and comes into no file name and no URL."""
    return isinstance(text, str) and DOMAIN.fullmatch(text) is not None


def file_name(domain):
    """The name of a domain's file. A domain whose first label is the name of a device on Windows (nul.ai) gets an
    underscore in front: no domain name holds an underscore, so no two domains share a file. A value that is no
    domain name (is_domain()) raises ValueError, wherever it came from: no file path is made of it."""
    if not is_domain(domain):
        raise ValueError(f"{str(domain)[:80]!r} is no domain name, so no file is made of it")
    return ("_" if domain.split(".")[0] in DEVICES else "") + domain + ".jsonl"


def domain_path(task, domain):
    return os.path.join(task, "domains", file_name(domain))


def add_record(task, domain, record):
    held(task)
    os.makedirs(os.path.join(task, "domains"), exist_ok=True)
    append_line(domain_path(task, domain), record)


def _records(task, domain):
    """The records of a domain's file, one by one. A line that cannot be read, as one that a stop cut short, is
    passed over."""
    path = domain_path(task, domain)
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line) if line.strip() else None
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def read_records(task, domain):
    return list(_records(task, domain))


def first_record(task, domain):
    """The first record of a domain's file, the one it came into the task with: the row of the data source, with
    its registration date. None without a file."""
    return next(_records(task, domain), None)


# ---------------------------------------------------------------- the archive
def retire_days(config):
    """retire_days of config.json: how many days a domain is watched, counted from its registration date. None where
    the field is missing: no end. Raises ValueError, with the message for the user, where it is not a number above
    0."""
    days = config.get("retire_days")
    if days is None:
        return None
    if isinstance(days, bool) or not isinstance(days, (int, float)) or days <= 0:
        raise ValueError(f"retire_days is {days!r}. It is the number of days a domain is watched, counted from its "
                         "registration date, a number above 0; leave the field out to watch without an end "
                         "(SKILL.md 1.5).")
    return days


def archived(task):
    """The domains in archive/state.csv: the ones that left the state machine."""
    path = os.path.join(task, "archive", "state.csv")
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8", newline="") as f:
        return {row["domain"] for row in csv.DictReader(f)}


def archive(task, rows, gone, at):
    """Takes the domains in gone out of the state machine. Each of their rows is appended to archive/state.csv as it
    stands, with the time; the domain's file is moved to archive/domains as it is; state.csv is written without
    them. Returns the rows that stay. A domain that is in the archive already, from a run that stopped half way, gets
    no second line."""
    held(task)
    os.makedirs(os.path.join(task, "archive", "domains"), exist_ok=True)
    path, known = os.path.join(task, "archive", "state.csv"), archived(task)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        out = csv.DictWriter(f, fieldnames=COLUMNS + ["archived"])
        if new:
            out.writeheader()
        out.writerows({**row, "action": cell(row["action"]), "archived": at}
                      for row in rows if row["domain"] in gone and row["domain"] not in known)
    for domain in gone:
        if os.path.exists(domain_path(task, domain)):
            os.replace(domain_path(task, domain), os.path.join(task, "archive", "domains", file_name(domain)))
    stay = [row for row in rows if row["domain"] not in gone]
    write_state(task, stay)
    return stay


def log(task, program, domain, step, result):
    """One line of log.jsonl per step. No page text, no keys, no reply bodies."""
    held(task)
    append_line(os.path.join(task, "log.jsonl"),
                {"at": fmt(utcnow()), "program": program, "domain": domain, "step": step, "result": result})


def add_find(task, at, domain, status, report):
    held(task)
    path = os.path.join(task, "found.csv")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        out = csv.writer(f)
        if new:
            out.writerow(["time", "domain", "status", "report"])
        out.writerow([at, domain, status, cell(report)])


def last_find(task, domain):
    """The last line of found.csv for a domain, as its time, the status then and the text; None where the user was
    told nothing about it."""
    path = os.path.join(task, "found.csv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", newline="") as f:
        lines = [line for line in csv.DictReader(f) if line.get("domain") == domain]
    return {"at": lines[-1]["time"], "status": lines[-1]["status"], "report": lines[-1]["report"]} if lines else None
