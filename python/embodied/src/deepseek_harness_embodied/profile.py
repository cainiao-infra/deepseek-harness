"""Install an explicitly selected source bundle in a new isolated Harness profile."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

BUNDLE = "@deepseek-ai/dsh-experimental-embodied-planner"


def install_source_profile(dsh_home: Path, bundle: Path) -> Path:
    """Create a new profile pointing to a built workspace bundle; never replace user profiles."""
    bundle = bundle.resolve(strict=True)
    manifest = json.loads((bundle / "package.json").read_text())
    if manifest.get("name") != BUNDLE or not (bundle / "lib/index.js").is_file():
        raise ValueError("A built embodied planner bundle is required")
    profile = dsh_home.resolve() / "profiles" / "embodied-planner"
    profile.mkdir(parents=True, exist_ok=False)
    (profile / "package.json").write_text(json.dumps({
        "private": True, "type": "module",
        "dependencies": {BUNDLE: "file:" + str(bundle)},
        "dsh": {"profile": {"bundles": [BUNDLE]}},
    }, indent=2) + "\n")
    link = profile / "node_modules" / "@deepseek-ai" / "dsh-experimental-embodied-planner"
    link.parent.mkdir(parents=True)
    link.symlink_to(bundle, target_is_directory=True)
    return profile


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsh-home", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    print(install_source_profile(args.dsh_home, args.bundle))
