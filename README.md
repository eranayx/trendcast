# TrendCast

## Inspiration
I barely keep up with the news myself. Wouldn't it be nice to have a way to stay up to date?

## What it does
TrendCast gives a quick, spoken snapshot of what tech-related things people are talking about across Reddit, YouTube, and Instagram. The TTS uses a friendly tone to simulate talking to a friend (so it's less awkward).

## How we built it
- **Data Pipeline**: Cartesia STT and TTS for natural conversation, text filtering, parsing, and summarization powered by openai/gpt-oss-20b via Groq, and Apify web scraping
- **Frontend**: React + TypeScript for a simple and clean UI/UX experience
- **Backend**: Python with FastAPI for API endpoints, Pydantic for request validation

## Challenges we ran into
- Empty responses: Apify actors often timed out or gave no response
- Messy data: Apify results were sometimes irrelevant to the search query
- Scraping delay: achieving a balance between delivering an accurate result quickly without sacrificing data

## Accomplishments that we're proud of
- Pipelining multiple APIs and ensuring accurate and useful responses
- Building a working MVP in a short amount of time

## What we learned
- Combining multiple APIs to create a cohesive program
- Cleaning API data to ensure information is usable

## What's next for TrendCast
- Including more sources to scrape for more accurate coverage
- Expand coverage past tech-only news
- Add more voices for variety.
