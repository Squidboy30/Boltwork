# Parsebit Phase 2 — Deployment Instructions
# =============================================
# Complete step-by-step guide to deploy Phase 2 (code review endpoints)
# without breaking Phase 1 (PDF summarisation).
#
# Prerequisites: Parsebit Phase 1 is live and working.
# Time estimate: 30-45 minutes.

## Step 1: Create the routers directory in your local repo

cd C:\Users\Ian\Desktop\summarise-api
mkdir routers

## Step 2: Copy the new files into your repo

# Copy these files from this package into your local repo:
#
#   parsebit-phase2/routers/review.py  -> summarise-api/routers/review.py
#   parsebit-phase2/main.py            -> summarise-api/main.py  (REPLACES existing)
#   parsebit-phase2/tests/test_review.py -> summarise-api/tests/test_review.py
#
# IMPORTANT: The new main.py is a DROP-IN REPLACEMENT.
# All existing code is identical — only two lines were added:
#   Line 14: from routers.review import router as review_router
#   Line 48: app.include_router(review_router)

## Step 3: Create the routers __init__.py

# Create an empty file at:
#   summarise-api/routers/__init__.py
# (This makes routers/ a Python package)

## Step 4: Run tests locally BEFORE deploying

cd C:\Users\Ian\Desktop\summarise-api
pip install -r requirements.txt
pytest tests/test_review.py -v

# All tests should pass. If any fail, DO NOT deploy until fixed.
# The tests mock the Anthropic API so no real API calls are made.

## Step 5: Verify summarise endpoints still work locally

# Start the server:
uvicorn main:app --reload --port 8000

# In another terminal, test existing endpoints still work:
curl http://localhost:8000/health
curl http://localhost:8000/.well-known/l402.json

# Confirm new endpoints exist (will get 422 with no body, NOT 404):
curl -X POST http://localhost:8000/review/code
curl -X POST http://localhost:8000/review/url

## Step 6: Deploy to Fly.io

cd C:\Users\Ian\Desktop\summarise-api
flyctl deploy --app parsebit --ha=false

# Watch the deploy logs — should complete in ~2 minutes.
# No config changes needed on the parsebit app itself.

## Step 7: Verify the live deployment

# Health check:
curl https://parsebit.fly.dev/health

# Confirm new endpoints are in the l402.json:
curl https://parsebit.fly.dev/.well-known/l402.json

# You should see /review/code and /review/url in the pricing array.

## Step 8: Update Aperture config (on parsebit-lnd)

flyctl ssh console --app parsebit-lnd

# Inside SSH, edit aperture.yaml:
vi /root/.lnd/.aperture/aperture.yaml

# Add the two new service entries from aperture-phase2.yaml:
#
#   - hostregexp: 'parsebit-lnd.fly.dev'
#     pathregexp: '^/review/code'
#     address: "parsebit.fly.dev"
#     protocol: https
#     price: 2000
#
#   - hostregexp: 'parsebit-lnd.fly.dev'
#     pathregexp: '^/review/url'
#     address: "parsebit.fly.dev"
#     protocol: https
#     price: 2000

# Restart Aperture to pick up the new config:
pkill aperture
aperture --config /root/.lnd/.aperture/aperture.yaml &

exit

## Step 9: End-to-end test of the code review flow

# Test that the 402 gate works for code review:
curl -v -X POST "https://parsebit-lnd.fly.dev/review/code" \
  -H "Content-Type: application/json" \
  -d "{\"code\": \"def hello(): pass\"}"

# Should return HTTP 402 with a 2000 sat invoice.

# Pay the invoice from Strike, then retry with L402 credentials:
# (Same flow as PDF summarisation but with 2000 sats)

## Step 10: Update .well-known/l402.json

# This is automatically served from the new main.py — no manual update needed.
# The new endpoint pricing entries are already in the code.

## Rollback plan

# If anything breaks, deploy the previous main.py:
git revert HEAD
flyctl deploy --app parsebit --ha=false

# The review.py router is completely isolated — reverting main.py to the
# previous version (without include_router) restores Phase 1 exactly.
# No database changes, no config changes, no state changes.

## What was changed (summary)

# Files ADDED (new):
#   routers/__init__.py      (empty, makes routers a package)
#   routers/review.py        (all Phase 2 logic, isolated)
#   tests/test_review.py     (35 tests covering review router)

# Files MODIFIED:
#   main.py                  (2 lines added: import + include_router)
#                            (version bumped to 2.0.0)
#                            (FastAPI title/description updated)
#                            (/health now returns version)
#                            (agent-spec.md updated with review docs)
#                            (l402.json updated with review pricing)

# Files NOT CHANGED:
#   Everything else in the repo is untouched.
#   Dockerfile, fly.toml, requirements.txt — all unchanged.
