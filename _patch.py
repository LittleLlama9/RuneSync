
path = r"C:\Users\Matth\RuneSync\lcu.py"
with open(path, "r", encoding="utf-8") as f:
    src = f.read()

new_method = '''    def get_current_rune_page(self):
        """Return the currently active rune page from the League client, or None."""
        try:
            pages = self._get("/lol-perks/v1/pages")
            for page in pages:
                if page.get("current", False) or page.get("isActive", False):
                    return page
            return pages[0] if pages else None
        except Exception:
            return None

    def import_rune_page('''

src = src.replace("    def import_rune_page(", new_method)

with open(path, "w", encoding="utf-8") as f:
    f.write(src)

if "get_current_rune_page" in src:
    print("SUCCESS - method patched")
else:
    print("FAILED")
