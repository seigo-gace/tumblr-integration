"""Owner-only CM link selection for the adopted Telegram announcement footer.

Uses the existing owner-provided SQLite connection. No second database,
advertising platform, click tracking, or Telegram delivery is created here.
Only the existing MPB owner may establish a verified delivery.
"""
import json
from urllib.parse import urlsplit


class CMRegistry:
    def __init__(self, db):
        self.db = db
        with db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cm_links (
                    cm_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    url TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    destination TEXT
                );
                CREATE TABLE IF NOT EXISTS cm_positions (
                    destination TEXT PRIMARY KEY,
                    offset INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS cm_advances (
                    operation_key TEXT PRIMARY KEY,
                    destination TEXT NOT NULL,
                    chosen_ids TEXT NOT NULL
                );
            """)

    @staticmethod
    def _link(label, url):
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 40:
            raise ValueError("cm_label_invalid")
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError("cm_url_invalid")
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            raise ValueError("cm_url_invalid") from None
        # HTTPS links only. Actual redirect/advertiser policy remains the
        # Telegram publisher owner's responsibility at publication time.
        if (parsed.scheme != "https" or not host or parsed.username or
                parsed.password or port not in (None, 443) or
                any(char.isspace() for char in url)):
            raise ValueError("cm_url_invalid")
        return label.strip(), url

    def add(self, cm_id, label, url, destination=None, *, enabled=True):
        if not isinstance(cm_id, str) or not 1 <= len(cm_id) <= 80:
            raise ValueError("cm_id_invalid")
        if destination is not None and (not isinstance(destination, str) or not destination):
            raise ValueError("cm_destination_invalid")
        label, url = self._link(label, url)
        if type(enabled) is not bool:
            raise ValueError("cm_enabled_invalid")
        with self.db:
            self.db.execute(
                "INSERT INTO cm_links(cm_id,label,url,enabled,destination) VALUES(?,?,?,?,?)",
                (cm_id, label, url, int(enabled), destination))

    def set_enabled(self, cm_id, enabled):
        if type(enabled) is not bool:
            raise ValueError("cm_enabled_invalid")
        with self.db:
            changed = self.db.execute(
                "UPDATE cm_links SET enabled=? WHERE cm_id=?",
                (int(enabled), cm_id)).rowcount
        if changed != 1:
            raise ValueError("cm_missing")

    def list_links(self):
        rows = self.db.execute(
            "SELECT cm_id,label,url,enabled,destination FROM cm_links ORDER BY cm_id"
        ).fetchall()
        return [{"id": r[0], "label": r[1], "url": r[2],
                 "enabled": bool(r[3]), "destination": r[4]} for r in rows]

    def select(self, destination):
        if not isinstance(destination, str) or not destination:
            raise ValueError("cm_destination_invalid")
        rows = self.db.execute(
            "SELECT cm_id,label,url FROM cm_links WHERE enabled=1 "
            "AND (destination IS NULL OR destination=?) ORDER BY cm_id",
            (destination,)).fetchall()
        if not rows:
            return []
        position = self.db.execute(
            "SELECT offset FROM cm_positions WHERE destination=?",
            (destination,)).fetchone()
        start = (position[0] if position else 0) % len(rows)
        return [{"id": rows[(start+i) % len(rows)][0],
                 "label": rows[(start+i) % len(rows)][1],
                 "url": rows[(start+i) % len(rows)][2]}
                for i in range(min(3, len(rows)))]

    def advance_after_delivery(self, destination, operation_key, chosen_ids, *,
                               verified=False):
        """Advance once, only after trusted MPB delivery confirmation.

        Preview never rotates. No Telegram send occurs in this module.
        Reusing the same operation ID never advances twice.
        """
        if verified is not True:
            return False
        if not isinstance(operation_key, str) or not operation_key:
            raise ValueError("cm_operation_invalid")
        if not isinstance(chosen_ids, (tuple, list)) or any(
                not isinstance(x, str) for x in chosen_ids):
            raise ValueError("cm_selection_invalid")
        ids = list(chosen_ids)
        if len(ids) > 3 or len(set(ids)) != len(ids):
            raise ValueError("cm_selection_invalid")
        with self.db:
            prior = self.db.execute(
                "SELECT destination,chosen_ids FROM cm_advances WHERE operation_key=?",
                (operation_key,)).fetchone()
            encoded = json.dumps(ids, separators=(",", ":"))
            if prior:
                if prior != (destination, encoded):
                    raise ValueError("cm_operation_conflict")
                return False
            current = [entry["id"] for entry in self.select(destination)]
            if current != ids:
                raise ValueError("cm_selection_stale")
            self.db.execute(
                "INSERT INTO cm_advances(operation_key,destination,chosen_ids) VALUES(?,?,?)",
                (operation_key, destination, encoded))
            if ids:
                self.db.execute(
                    "INSERT INTO cm_positions(destination,offset) VALUES(?,1) "
                    "ON CONFLICT(destination) DO UPDATE SET offset=offset+1",
                    (destination,))
            return bool(ids)


def append_cm_buttons(existing_rows, selected):
    """Preserve all existing MPB rows, then append at most three PR URL rows."""
    if not isinstance(existing_rows, list) or not isinstance(selected, list):
        raise ValueError("cm_keyboard_invalid")
    if len(selected) > 3:
        raise ValueError("cm_selection_invalid")
    rows = [list(row) for row in existing_rows]
    for item in selected:
        label, url = CMRegistry._link(item["label"], item["url"])
        rows.append([{"text": "PR / " + label, "url": url}])
    return rows
