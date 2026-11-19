"""The entrance (SKILL.md section 3): python scripts\\pull.py <task folder>

Reads the source section again when it is missing or older than 5 days, runs the pulls of config.json against the
data source, and adds the domains that are new to state.csv, due at once. A domain on exclude or in the archive is not
brought in, and neither is one that was registered retire_days ago or longer, where config.json holds that field: the
check program would move it to the archive unchecked. A keyword or TLD pull asks nrds for the domains registered in
the last window_days days (3 where config.json does not set the field) and reads them to the end, at each pull, so
that a domain whose record has come to match since the last pull is brought in. A lookalike pull asks typosquat for
the variants of each brand domain and takes those that are registered, whatever their registration date: the first
pull brings in the ones that exist, later pulls the ones registered since. Each domain that is new to the task is
brought in: no number limits what a pull adds. An entry's side (typo or business), where it names one, is written
into the source record of each domain it brings in. The keywords, TLDs and brand domains take turns, one new domain
each, so the new domains of one do not stand in state.csv before those of the others: the check takes the rows that
came in first. Where config.json holds pull_every, the pull is made only when that time has passed since the last one, which
the program notes as last_pull in config.json: so the scheduler can start the program more often than the pull is
due. A value of the data source that is no domain name is not brought in. Exit code 0 when done or not due; 1 when it
did not start (no config.json in the folder, a config.json it cannot work with, a run in progress, the key not set),
when the data source failed, or when another start took its lock over while it was at work."""
import itertools
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

import requests

import common

PAGE_ROWS = 10     # rows on a full nrds page
WINDOW_DAYS = 3    # how many days back a pull looks where config.json sets no window_days


class SourceError(Exception):
    """The data source failed or refused a call."""


class ConfigError(Exception):
    """config.json does not hold what a pull needs."""


class DomainKits:
    """The data source: MCP JSON-RPC over HTTPS, key in X-API-Key, one call at a time."""
    URL = "https://api.domainkits.com/v1/mcp"
    PROTOCOL = "2025-06-18"
    ATTEMPTS = 3

    def __init__(self, api_key):
        self.http, self.ready, self.id = requests.Session(), False, 0
        self.headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                        "X-API-Key": api_key}

    @staticmethod
    def _parse(resp):
        if "text/event-stream" in resp.headers.get("Content-Type", ""):
            msgs = [json.loads(line[5:].strip()) for line in resp.text.splitlines()
                    if line.startswith("data:") and line[5:].strip()]
            return msgs[-1] if msgs else None
        return resp.json() if resp.text.strip() else None

    def _post(self, payload):
        err = "no attempt"
        for attempt in range(1, self.ATTEMPTS + 1):
            try:
                # No redirect is followed: the key travels in a header of its own, which a redirect would carry to
                # whatever host it names.
                r = self.http.post(self.URL, headers=self.headers, json=payload, timeout=60, allow_redirects=False)
            except requests.RequestException as e:
                err = type(e).__name__
            else:
                if 300 <= r.status_code < 400:
                    raise SourceError(f"HTTP {r.status_code}: the data source answered with a redirect, which is not "
                                      "followed, so the key goes to no other host")
                if r.status_code == 401:
                    raise SourceError("DomainKits rejected the API key (401)")
                if r.status_code == 429:
                    err = "HTTP 429"
                    wait = r.headers.get("Retry-After", "")
                    if attempt < self.ATTEMPTS:
                        time.sleep(min(int(wait), 60) if wait.isdigit() else 5 * attempt)
                    continue
                if r.status_code >= 500:
                    err = f"HTTP {r.status_code}"
                elif r.status_code >= 400:
                    raise SourceError(f"HTTP {r.status_code}: {r.text[:200]}")
                else:
                    if r.headers.get("Mcp-Session-Id"):
                        self.headers["Mcp-Session-Id"] = r.headers["Mcp-Session-Id"]
                    return self._parse(r)
            if attempt < self.ATTEMPTS:
                time.sleep(5 * attempt)
        raise SourceError(f"DomainKits unreachable: {err}")

    def _rpc(self, method, params=None):
        self.id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self.id, "method": method, "params": params or {}})
        if not msg:
            raise SourceError(f"{method}: empty response")
        if "error" in msg:
            raise SourceError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result", {})

    def _init(self):
        if self.ready:
            return
        res = self._rpc("initialize", {"protocolVersion": self.PROTOCOL, "capabilities": {},
                                       "clientInfo": {"name": "new-sites", "version": "0"}})
        self.headers["MCP-Protocol-Version"] = res.get("protocolVersion", self.PROTOCOL)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.ready = True

    def call(self, tool, args=None):
        self._init()
        res = self._rpc("tools/call", {"name": tool,
                                       "arguments": {k: v for k, v in (args or {}).items() if v not in (None, "")}})
        data = res.get("structuredContent")
        if data is None:
            text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
            try:
                data = json.loads(text)
            except ValueError:
                data = {"message": text}
        blob = json.dumps(data).lower()
        if res.get("isError") or data.get("success") is False or "limit_reached" in blob:
            raise SourceError(f"{tool}: {str(data.get('error') or data.get('message') or blob)[:200]}")
        return data

    def usage(self):
        return self.call("usage")

    def nrds_params(self):
        """The names of the nrds tool's parameters, in the order of its schema."""
        self._init()
        tools = self._rpc("tools/list").get("tools", [])
        schema = next((t.get("inputSchema") or {} for t in tools if t.get("name") == "nrds"), {})
        return list(schema.get("properties") or {})

    def nrds(self, args):
        return self.call("nrds", args)

    def typosquat(self, args):
        return self.call("typosquat", args)


def source_section(usage, params, today):
    """config["source"] from the usage reply and the nrds parameter names."""
    groups = {t: g for g in usage.get("tools", []) for t in g.get("tools", [])}
    nrds = groups.get("nrds", {})
    filters = (nrds.get("filters") or {}).get("nrds", [])
    return {"read_on": today,
            "nrds_filters": list(params) if filters == "all" else list(filters or []),
            "nrds_cctld": (nrds.get("cctld") or {}).get("nrds", []),
            "counting": nrds.get("cost_per_call") or None,
            "limits": {"daily_remaining": None if nrds.get("daily_unlimited") else nrds.get("daily_remaining"),
                       "rate_per_minute": None if nrds.get("rate_unlimited") else nrds.get("rate_per_minute")}}


def pull(task, config, source, clock):
    """Runs the pulls. Each keyword, TLD and brand domain is one query, read as a stream of rows that calls its next
    page only when it needs more rows. The streams take turns: in its turn a stream adds at most one new domain and
    passes over the rows that are empty, in state.csv already, on exclude, in the archive or past retire_days, until
    each stream is used up. Returns the counts: calls, rows (seen), added."""
    def note(step, result, domain=""):
        common.log(task, "pull", domain, step, result)

    # How long a domain is watched (retire_days) counts from its registration date. A domain that is past it when a
    # pull sees it would go into the archive at the next check run, without a check: it is not brought in. A row
    # without a registration date comes in and is counted from then. Checked before any call to the data source.
    try:
        retire = common.retire_days(config)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    now, late = clock(), set()

    def too_old(row):
        try:
            day = datetime.strptime(str(row.get("registered_date")), "%Y-%m-%d")
        except ValueError:
            return False
        return day + timedelta(days=retire) <= now

    # A protection task judges and tells by the side a domain came in from (protection/SKILL.md 2.2 and 2.4), so
    # each of its pulls has to name one. Checked before any call to the data source.
    if config.get("skill") == "protection":
        for entry in config["pulls"]:
            sides = ("typo",) if entry.get("kind") == "lookalike" else ("typo", "business")
            if entry.get("side", "typo" if entry.get("kind") == "lookalike" else None) not in sides:
                raise ConfigError(f"a pull of a protection task names its side: the {entry.get('kind')} pull with "
                                  f"{entry.get('keywords') or entry.get('tlds') or entry.get('domains')} needs "
                                  "\"side\": \"typo\" (names close to the user's) or \"side\": \"business\" (core "
                                  "keywords of the product); a lookalike pull is of the typo side")

    today = clock().date()
    src = config.get("source")
    if not src or (today - date.fromisoformat(src["read_on"])).days > 5:
        src = config["source"] = source_section(source.usage(), source.nrds_params(), today.isoformat())
        common.save_config(task, config)
        note("source", f"read again on {today}")

    # The window is the user's: the domains registered in the last window_days days, 3 where config.json does not
    # set it, asked for by registration date. A pull reads them to the end and brings in each one that is new, at
    # each pull: no other number bounds it. So a domain that comes to match days after it was registered (its
    # nameservers were moved, a mail server was set) is brought in at the next pull, wherever it stands among the
    # rows the task knows already: that is the polling this design is for. After a pause longer than the window, as
    # when the machine was off, the pull starts over with the window of that day and does not read back: what was
    # registered before it is past the window the user chose, and this agent is about being early.
    days = config.get("window_days", WINDOW_DAYS)
    window = {"registered_from": (today - timedelta(days=days - 1)).isoformat(), "registered_to": today.isoformat()}
    n = {"calls": 0, "rows": 0, "added": 0}

    def nrds_rows(query, extra):
        """The rows of one nrds query, as (what the source record adds, row), page by page to the end of the
        window."""
        item = query.get("keyword") or query.get("tld")
        before = None
        for page in itertools.count(1):
            common.beat(task)     # a pull that pages long without a new domain is at work too
            res = source.nrds({**query, "page": str(page)})
            data = [row for row in res.get("data") or [] if isinstance(row, dict)]
            n["calls"] += 1
            n["rows"] += len(data)
            note("nrds", f"{item} page {page}: {len(data)} rows")
            names = [row.get("domain") for row in data]
            if names == before:     # the same rows again: the data source did not move on, so the reading stops
                note("nrds", f"{item} page {page}: the rows of page {page - 1} again, reading stops")
                return
            before = names
            for row in data:
                yield extra, row
            try:
                total = int(res.get("total_found"))
            except (TypeError, ValueError):
                total = None     # without a total, a page with fewer rows than a full one ends the window
            if len(data) < PAGE_ROWS or (total is not None and page * PAGE_ROWS >= total):
                return

    def lookalike_rows(brand, extra, since):
        """The variants of one brand domain that are registered, newest first, as (what the source record adds,
        row): one typosquat call, no paging. A variant counts as registered when its row carries a registration
        date. The window does not apply: the first pull brings in the variants that exist, later pulls the ones
        registered since. Where the entry gives registered_from, the variants registered before that date are left
        out. The source record carries the brand, so the model can see what the variant was made from."""
        common.beat(task)
        res = source.typosquat({"domain": brand})
        registered = sorted((r for r in res.get("data") or []
                             if r.get("registered_date") and str(r["registered_date"]) >= since),
                            key=lambda r: str(r["registered_date"]), reverse=True)
        n["calls"] += 1
        n["rows"] += len(registered)
        note("typosquat", f"{brand}: {res.get('total')} variants, {len(registered)} registered"
             + (f" since {since}" if since else ""))
        for row in registered:
            yield {"of": brand, **extra}, row

    streams = []
    for entry in config["pulls"]:
        if entry["kind"] not in ("keyword", "tld", "lookalike"):
            note("pull", f"kind {entry['kind']} is not built in v0")
            continue
        filters = {}
        for name, value in (entry.get("filters") or {}).items():
            if entry["kind"] == "lookalike":
                note("filter", f"{name} left out: a lookalike pull takes no filters")
            elif name in src["nrds_filters"]:
                filters[name] = value
            else:
                note("filter", f"{name} left out: not in source.nrds_filters")
        field = "domain" if entry["kind"] == "lookalike" else entry["kind"]
        items = entry.get(field + "s")
        if not isinstance(items, list) or not items:
            raise ConfigError(f"a pull of kind {entry['kind']} needs the field {field}s, a list with at least one "
                              "entry")
        # The side a domain came in from goes into its source record: an entry names it (typo or business, as the
        # protection skill writes them); a lookalike pull is of the typo side by what it is.
        side = entry.get("side") or ("typo" if entry["kind"] == "lookalike" else "")
        extra = {"side": side} if side else {}
        if entry["kind"] == "lookalike":
            streams += [lookalike_rows(item, extra, str(entry.get("registered_from") or "")) for item in items]
        else:
            streams += [nrds_rows({**filters, field: item, **window}, extra) for item in items]

    rows = common.read_state(task)
    # A domain in the archive has left the task (retire_days): it is not brought in again.
    skip = {r["domain"] for r in rows} | {d.strip().lower() for d in config["exclude"]} | common.archived(task)
    while streams:
        for stream in list(streams):
            for extra, row in stream:
                domain = str(row.get("domain", "")).strip().lower()
                if not domain or domain in skip:
                    continue
                if not common.is_domain(domain):
                    # A value that is no domain name comes into no file name and no URL.
                    note("skip", f"no domain name: {domain[:80]!r}")
                    skip.add(domain)
                    continue
                if retire and too_old(row):
                    late.add(domain)
                    continue
                at = common.fmt(clock())
                common.beat(task)
                common.add_record(task, domain, {"at": at, "kind": "source", **extra, "row": row})
                rows.append({"domain": domain, "last-check": "", "status": "", "action": "first check",
                             "action-date": at})
                common.write_state(task, rows)
                skip.add(domain)
                n["added"] += 1
                note("added", "new row, due at once", domain)
                break
            else:
                streams.remove(stream)
    if late:
        note("retire", f"{len(late)} domains not brought in: registered {retire} days ago or longer (retire_days)")
    return n


def main(argv=None, source=None, clock=common.utcnow):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python pull.py <task folder>")
        return 1
    task = os.path.abspath(args[0])
    missing = common.not_set_up(task)
    if missing:
        print(missing)
        return 1
    if not common.lock(task):
        return 1
    try:
        try:
            config = common.load_config(task)
        except ValueError as e:
            print(f"config.json cannot be read as JSON: {e}")
            return 1
        problems = common.config_problems(config, "pull") if isinstance(config, dict) else ["it holds no object"]
        if problems:
            print("config.json: " + "; ".join(problems) + ".")
            return 1
        every, last = config.get("pull_every"), config.get("last_pull")
        if every and last and common.parse(last) + common.duration(every) > clock():
            print(f"pull: not due before {common.fmt(common.parse(last) + common.duration(every))} "
                  f"(pull_every {every})")
            return 0
        if source is None:
            if not isinstance(config["keys"].get("data"), str):
                print("config.json: keys lacks data, the name of the variable that holds the key of the data source "
                      "(SKILL.md 1.7).")
                return 1
            api_key = common.key(task, config, "data")
            if not api_key:
                print(common.unset(task, config, [config["keys"]["data"]]))
                return 1
            source = DomainKits(api_key)
        try:
            n = pull(task, config, source, clock)
        except SourceError as e:
            print(f"The data source failed: {e}")
            return 1
        except ConfigError as e:
            print(f"config.json: {e}")
            return 1
        if every:
            # Read again: the user or the assistant may have changed the file while the pull was at work, and
            # only last_pull is the program's to write here.
            fresh = common.load_config(task)
            fresh["last_pull"] = common.fmt(clock())
            common.save_config(task, fresh)
        print(f"pull: {n['calls']} calls, {n['rows']} rows seen, {n['added']} domains added")
        return 0
    except common.LockLost as e:
        print(f"Stopped: {e}")
        return 1
    finally:
        common.unlock(task)


if __name__ == "__main__":
    sys.exit(main())
