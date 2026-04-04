import os

target = "/Users/qianj/AntigravityProjects/NanoPulse/src/ui/widgets/contracts_view.py"
top = "/Users/qianj/AntigravityProjects/NanoPulse/tmp_top_half.py"

with open(target, "r") as f:
    orig = f.read()

with open(top, "r") as f:
    new_top = f.read()

# Find the split point in the original file
split_marker = "    # ── Find handlers (one per section) ───────────────────────────────────────"
if split_marker not in orig:
    raise ValueError("Could not find the split marker in contracts_view.py")

parts = orig.split(split_marker)
bottom_half = split_marker + parts[1]

# In the bottom half, we need to modify exactly one thing:
# The `BrowseDialog` styles currently use `BG_PRIMARY`, `BORDER`, etc which were defined globally.
# We inject the Theme references into _BROWSE_TABLE_STYLE.
bottom_half = bottom_half.replace("{BG_PRIMARY}", "{Theme.BG_MAIN}")
bottom_half = bottom_half.replace("{BG_SECONDARY}", "{Theme.BG_PANEL}")
bottom_half = bottom_half.replace("{BG_HEADER}", "{Theme.BG_INPUT}")
bottom_half = bottom_half.replace("{BORDER}", "{Theme.BORDER}")
bottom_half = bottom_half.replace("{TEXT}", "{Theme.TEXT}")
bottom_half = bottom_half.replace("{TEXT_DIM}", "{Theme.TEXT_DIM}")
bottom_half = bottom_half.replace("{GREEN}", "{Theme.ACCENT}")
bottom_half = bottom_half.replace("{ORANGE}", '"{Theme.ACCENT}"') # was ORANGE, use accent

# Construct final
final_code = new_top + "\n" + bottom_half

with open(target, "w") as f:
    f.write(final_code)

print("Contracts View successfully rewritten!")
