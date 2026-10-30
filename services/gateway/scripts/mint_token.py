"""Mint a dev JWT for calling the gateway by hand.

JWT_SECRET=... uv run python services/gateway/scripts/mint_token.py --sub alice --role admin
"""

import argparse
import os

from gateway.auth import mint_token

ap = argparse.ArgumentParser()
ap.add_argument("--sub", default="dev-analyst")
ap.add_argument("--role", choices=["analyst", "admin"], default="analyst")
args = ap.parse_args()
print(mint_token(os.environ["JWT_SECRET"], args.sub, args.role))
