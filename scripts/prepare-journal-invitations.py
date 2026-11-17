"""Save/reuse one private 1Password invitation per member; emit only hash registration SQL."""
import argparse
import hashlib
import json
import re
import secrets
import subprocess


def op(*args, template=None):
    result = subprocess.run(["op", *args, "--format", "json"], input=template,
                            text=True, stdout=subprocess.PIPE, check=True)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, help="Owen's private 1Password vault")
    args = parser.parse_args()
    items = op("item", "list", "--vault", args.vault)
    statements = []
    for member in ("Owen", "Jayden", "Jacob"):
        title = f"Options View private journal invitation — {member}"
        matches = [item for item in items if item["title"] == title]
        if len(matches) > 1:
            raise SystemExit(f"Resolve duplicate items for {member} before continuing.")
        if matches:
            item_id = matches[0]["id"]
        else:
            template = {"title": title, "category": "LOGIN", "fields": [
                {"id": "username", "type": "STRING", "purpose": "USERNAME", "value": member},
                {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "value": secrets.token_urlsafe(32)}],
                "urls": [{"primary": True, "href": "https://options.moopertonic.net/trade-journal.html"}]}
            item_id = op("item", "create", "--vault", args.vault, "-", template=json.dumps(template))["id"]
        item = op("item", "get", item_id, "--vault", args.vault)
        code = next(field["value"] for field in item["fields"] if field["id"] == "password")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", code):
            raise SystemExit(f"Invalid saved invitation format for {member}.")
        digest = hashlib.sha256(code.encode("ascii")).hexdigest()
        statements.append("INSERT OR IGNORE INTO trade_journal_members(member,invite_hash,invite_until) "
                          f"VALUES ('{member}','{digest}',unixepoch()+604800);")
    print("\n".join(statements))


if __name__ == "__main__":
    main()
