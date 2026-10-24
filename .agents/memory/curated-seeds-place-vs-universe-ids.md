# Curated seed IDs must be universe IDs, and must be verified

Two separate traps in `game_search.CURATED_SEED_IDS`, both found live on
2026-09-25:

1. **Place IDs are not universe IDs.** The original list used famous
   *place* IDs (Adopt Me! 920587237, Jailbreak 606849621, MeepCity
   370731277, MM2 142823291 …) as universe IDs. The batch API happily
   returns a row for them — but it's some unrelated game ("morning",
   "cly's Place", 0 CCU). Nothing errors; every scan just enriches junk.
   Resolve a place ID to its universe first:
   `GET apis.roblox.com/universes/v1/places/{placeId}/universe` →
   `{"universeId": ...}`.

2. **Universe IDs die.** Several real universe IDs (The Strongest
   Battlegrounds-era list entries, Tower Defense Simulator, Dress to
   Impress's old ID, …) now return no data at all.

Rule: after touching the seed list, run `python validate_seeds.py` — it
probes every ID against the live batch API and exits 1 if any are dead.
