"""What messages mean: turn Discord messages into orders, hold them, search and group them.

This file never imports discord, so everything in it can be tested with fake messages
(see tests/test_jigging_analysis.py).

Orders live in memory only - there is no database. Every app run starts fresh; nothing
from a previous run is remembered. That's a deliberate simplification, not an oversight.
"""
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# Order: the data every other class passes around
# ---------------------------------------------------------------------------
@dataclass
class Order:
    message_id: str            # Discord's message ID: the one and only duplicate check
    channel_id: str
    channel_name: str
    profile: str
    status: str                # the embed title, as the bot wrote it (see OrderParser for the one fix)
    timestamp: datetime        # timezone-aware UTC, straight from message.created_at
    proxy_host: str = ""       # which proxy PROVIDER was used - host only, see OrderParser._proxy_host


# ---------------------------------------------------------------------------
# DateRange: "2026-09-16" -> a half-open range [start_day, end_day)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DateRange:
    start_day: "datetime | None" = None
    end_day: "datetime | None" = None

    @staticmethod
    def _day_start(text):
        # "2026-09-16" -> local midnight, made timezone-aware
        return datetime.strptime(text, "%Y-%m-%d").astimezone()

    @classmethod
    def from_strings(cls, start_text=None, end_text=None):
        start_day = cls._day_start(start_text) if start_text else None
        # the end day is included, so the range stops at the start of the NEXT day
        end_day = cls._day_start(end_text) + timedelta(days=1) if end_text else None
        if start_day and end_day and start_day >= end_day:
            raise ValueError("'from' date must be on or before 'to' date")
        return cls(start_day, end_day)

    def contains(self, moment):
        return (self.start_day is None or moment >= self.start_day) and \
               (self.end_day is None or moment < self.end_day)


# ---------------------------------------------------------------------------
# OrderParser: a Discord message -> an Order (or None)
# ---------------------------------------------------------------------------
class OrderParser:
    PROFILE_LABELS = {"profile", "profile name"}      # exact names, after simplifying
    PROXY_LABELS = {"proxy", "proxy group", "proxies"}
    SKIP_TITLES = ("Drop Summary",)                   # known non-order messages
    CANCEL_REASON_LABEL = "cancel reason"
    CANCELED_PREFIX = "Order Canceled: "

    # Different bots word the same outcome differently - e.g. one cancels with
    # Cancel Reason "Policy - Item Demand" while another titles it plain
    # "Order Canceled: Item Demand"; one reports a hold as Cancel Reason
    # "REVIEW_HOLD", another titles it "Successful Checkout (Review Hold)"
    # outright. Each tuple is (substring to look for, canonical status to use
    # instead) - checked in order, case-insensitively, against the status
    # AFTER the Hayha cancel-reason rewrite above. First match wins.
    STATUS_ALIASES = (
        ("review hold", "Review Hold"),
        ("review_hold", "Review Hold"),
        ("item demand", "Order Canceled: Item Demand"),
        ("quantity limit", "Order Canceled: Quantity Limit"),
        ("card was declined", "Order Canceled: Card Declined"),
        ("payment declined", "Order Canceled: Card Declined"),
        ("card declined", "Order Canceled: Card Declined"),
    )

    def parse_order(self, message):
        """Return an Order, or None if the message isn't an order."""
        for embed in message.embeds:
            status = (embed.title or "").strip()
            if not status:
                continue
            profile, cancel_reason, proxy_host = "", "", ""
            for field in embed.fields:
                name = self._simplify_label(field.name)
                if name in self.PROFILE_LABELS:
                    profile = self._remove_spoiler_bars(field.value)
                elif name == self.CANCEL_REASON_LABEL:
                    cancel_reason = self._remove_spoiler_bars(field.value)
                elif name in self.PROXY_LABELS:
                    proxy_host = self._proxy_host(field.value)
            if profile:
                # Hayha titles a canceled order "Successful Checkout!" and adds a Cancel Reason
                # field. Shikari and Valor already say "canceled" or "declined" in the title.
                if cancel_reason and "successful" in status.lower():
                    status = self.CANCELED_PREFIX + cancel_reason
                status = self._normalize_status(status)
                return Order(str(message.id), str(message.channel.id), message.channel.name,
                             profile, status, message.created_at, proxy_host)
        return None

    def should_skip(self, message):
        """True for messages that are known not to be orders, like a Drop Summary."""
        return any(marker in (embed.title or "")
                   for embed in message.embeds
                   for marker in self.SKIP_TITLES)

    @staticmethod
    def describe_structure(message):
        """Structure of a message for debugging: embed titles and field NAMES, never values."""
        return [(e.title, [f.name for f in e.fields]) for e in message.embeds]

    @classmethod
    def _normalize_status(cls, status):
        """Collapse known cross-bot wording variants onto one canonical status,
        so the same real-world outcome doesn't show up as separate near-duplicate
        entries in the status list (see STATUS_ALIASES above)."""
        s = status.lower()
        for substring, canonical in cls.STATUS_ALIASES:
            if substring in s:
                return canonical
        return status

    @staticmethod
    def _simplify_label(label):
        """'Profile Name:' -> 'profile name'"""
        return " ".join((label or "").lower().rstrip(": ").split())

    @staticmethod
    def _remove_spoiler_bars(value):
        """'|| Green Business W# 480 ||' -> 'Green Business W# 480'"""
        value = (value or "").strip()
        if value.startswith("||") and value.endswith("||"):
            value = value[2:-2]
        return value.strip()

    @classmethod
    def _proxy_host(cls, value):
        """The Proxy field is a full connection string: "host:port:username:password" -
        the username/password segment is a live proxy credential, not something to ever
        store or display. Only the host (which provider) is kept; everything else -
        port, username, password, session/targeting info baked into them - is discarded
        immediately and never returned, logged, or stored anywhere."""
        value = cls._remove_spoiler_bars(value)
        if not value:
            return ""
        return value.split(":", 1)[0].strip()


# ---------------------------------------------------------------------------
# OrderStore: everything for this run, in memory
#
# The Discord thread adds orders while web requests search them, so every
# method takes the lock, and methods that return a collection return a COPY.
# ---------------------------------------------------------------------------
class OrderStore:
    DEFAULT_SEARCH_TERMS = ["Co Mary", "Kem", "Jayden", "Cau Hung"]

    def __init__(self):
        self._orders = {}                                    # message_id -> Order
        self._search_terms = list(self.DEFAULT_SEARCH_TERMS)
        self._selected_ids = set()                           # channels the live listener watches
        self._lock = threading.Lock()

    def record(self, order):
        """Store an order. True if it's new, False if that message ID is already stored."""
        with self._lock:
            if order.message_id in self._orders:
                return False
            self._orders[order.message_id] = order
            return True

    def search(self, terms, date_range=None):
        """Orders whose profile contains ANY term (case-insensitive), oldest first.

        No terms means every order, not none.
        """
        wanted = [t.strip().lower() for t in (terms or []) if t and t.strip()]
        with self._lock:
            orders = list(self._orders.values())             # copy now, filter outside the lock
        found = []
        for order in orders:
            if wanted and not any(t in order.profile.lower() for t in wanted):
                continue
            if date_range and not date_range.contains(order.timestamp):
                continue
            found.append(order)
        return sorted(found, key=lambda o: o.timestamp)

    def latest_timestamp(self, channel_id):
        """Time of the newest stored order in a channel, or None. Used by incremental scans."""
        with self._lock:
            times = [o.timestamp for o in self._orders.values()
                     if o.channel_id == str(channel_id)]
        return max(times, default=None)

    def get_search_terms(self):
        with self._lock:
            return list(self._search_terms)

    def set_search_terms(self, terms):
        with self._lock:
            self._search_terms = list(terms)

    def get_selected_ids(self):
        with self._lock:
            return set(self._selected_ids)

    def set_selected_ids(self, ids):
        with self._lock:
            self._selected_ids = set(ids)

    def count(self):
        with self._lock:
            return len(self._orders)


# ---------------------------------------------------------------------------
# OrderGrouper: arrange a list of orders into the groups the page shows
#
# It returns only strings, numbers, lists and dicts, so json.dumps can
# encode the result directly.
# ---------------------------------------------------------------------------
class OrderGrouper:
    def __init__(self, orders):
        self.orders = orders

    def profiles_per_status(self):
        """{status: [profile, ...]}. Each profile appears ONCE per status."""
        groups = {}
        for o in self.orders:
            groups.setdefault(o.status, set()).add(o.profile)
        return {status: sorted(profiles) for status, profiles in sorted(groups.items())}

    def orders_per_profile(self):
        """{profile: {total, statuses, history}}. Counts EVERY order."""
        result = {}
        for o in self.orders:
            entry = result.setdefault(o.profile, {"total": 0, "statuses": {}, "history": []})
            entry["total"] += 1
            entry["statuses"][o.status] = entry["statuses"].get(o.status, 0) + 1
            entry["history"].append({"status": o.status, "timestamp": o.timestamp.isoformat()})
        return result

    def channels_per_profile(self):
        """{profile: [channel_name, ...]}"""
        groups = {}
        for o in self.orders:
            groups.setdefault(o.profile, set()).add(o.channel_name)
        return {profile: sorted(names) for profile, names in sorted(groups.items())}

    def orders_per_proxy_host(self):
        """{proxy_host: order count}, sorted by count descending - "which proxy
        provider gets the most orders in" (success or cancel both count: getting
        the order placed at all is what's being measured, not whether it held).
        Orders with no proxy info (field missing/unrecognized) are left out."""
        counts = {}
        for o in self.orders:
            if o.proxy_host:
                counts[o.proxy_host] = counts.get(o.proxy_host, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))

    def count_per_search_term(self, terms):
        """{term: how many orders matched}. A 0 shows a mistyped search word."""
        wanted = [t.strip() for t in (terms or []) if t and t.strip()]
        return {t: sum(1 for o in self.orders if t.lower() in o.profile.lower()) for t in wanted}
