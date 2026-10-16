# Smart Trend Stories

Flow per trend: **verify (6 steps) -> AI plan -> licensed photo -> mood design -> verify (2 steps) -> queue**.
Full report is stored in the job's `trend.verification`.

## Moods
solemn (black, B&W portrait, condolence only) | celebration/congrats | festival | sports | game | music | movie | tech | general

## Verification (trends/verify.py)
1 sources (>=2 publishers, https) | 2 fresh | 3 demand | 4 cross_check (topic words in 2+ publishers' headlines)
5 no_rumor (rumour/hoax/alleged/unconfirmed...) | 6 hard_block (politics, courts, crime, war/terror, communal, adult, betting/finance, suicide, minors)
7 classified (AI: not sensitive, confidence >= 0.7) | tone_match (loss wording can only get the solemn design)
8 confirmed (solemn only: AI says confirmed AND >=3 publishers)

## Photos
Only Wikimedia Commons files licensed CC0 / PD / CC BY / CC BY-SA, credit printed on the card.
No scraping, no AI-generated likeness of real people. Pass `user_photo=` to `build_story` for your own photos.

## Settings
`OPENROUTER_API_KEY` (env), `TREND_SMART=True`, `TREND_ALLOW_SOLEMN=True`. No AI key -> safe keyword fallback
(sports/music/movie/tech/festival only; never solemn). Docker: needs fonts-noto-color-emoji + libraqm0 (already added).

Preview: `python tools/preview_stories.py out/`
