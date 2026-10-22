"""Health check for step 2.   python -m story_engine.discovery.check

Calls every enabled source once (real network, NO database) and prints which feeds are alive, how many
candidates each gave, and the top merged events. Use it after editing config.DISCOVERY_FEEDS, and whenever
a feed seems dead. Feed URLs in config are not guaranteed forever; this tells you which ones to fix.
"""
import logging

from . import run


def main():
    logging.basicConfig(level=logging.WARNING)
    events, report = run.discover_report()
    print(f"{'source':44} {'items':>5}  status")
    for r in report:
        print(f"{r['source'][:44]:44} {r['candidates']:>5}  {'ERROR ' + r['error'] if r['error'] else 'ok'}")
    print(f"\n{len(events)} merged events; top 10 by signal:")
    for e in sorted(events, key=lambda x: -x["signal"])[:10]:
        pubs = ", ".join(s["name"] for s in e["sources"][:4])
        print(f"  [{e['category']:8}] {e['signal']:5.1f}  {len(e['sources'])} pub  {e['title'][:70]}  ({pubs})")


if __name__ == "__main__":
    main()
