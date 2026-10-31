"""Check that this machine can reach the county page. Prints the Measure Y row; writes nothing."""

import sys
import urllib.error

import county


def main():
    try:
        html = county.fetch()
    except urllib.error.HTTPError as e:
        print(f"Fetch failed: HTTP {e.code}")
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"Fetch failed: {getattr(e, 'reason', e)}")
        return 1
    except county.ParseError as e:
        print(f"Fetch failed: {e}")
        return 1
    print(f"Fetched county page ({len(html):,} characters)")

    try:
        r = county.parse(html)
    except county.ParseError as e:
        print(f"Parse failed: {e}")
        return 1

    print(f"Website Updated:  {r['county_updated']}")
    print(f"Ballots Counted:  {r['county_ballots_counted']:,}")
    print(f"Contest:          {r['contest']}")
    print(f"YES:              {r['yes']:,}")
    print(f"NO:               {r['no']:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
