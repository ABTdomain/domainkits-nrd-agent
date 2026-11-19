# domainkits-nrd-agent

An agent that finds new sites on newly registered domains and keeps watching them, built on [DomainKits](https://domainkits.com) data. It is an open reference design: you run it on your own machine, with your own keys, and change it for your own work.

## What it does

DomainKits keeps the mail servers, the nameservers and the registration date of newly registered domains current. The agent uses them as filters: they cut down how much has to be searched before a single page is read, so fetching and model calls are spent on a smaller set. A filter says nothing about whether a site is in use. That is decided by reading the page.

Each task is one direction of one user, with its own state machine. A domain is in one of five states:

| State | Meaning |
|---|---|
| `in_use` | A site is in use and fits what the task is after |
| `preparing` | Nothing visible yet, or getting ready: a default page, "coming soon" (two tiers, high and low) |
| `parking_sale` | Parked or offered for sale |
| `unrelated` | A page shows what the domain is for, and it does not fit the task |
| `unknown` | No usage state: nothing was fetched, or the reply was not enough to judge |

Two skills run on the same programs:

| Skill | For |
|---|---|
| [SKILL.md](SKILL.md) | New businesses in an industry, competitors, new projects and startups |
| [protection/SKILL.md](protection/SKILL.md) | A brand: the typos of your domain, new registrations that hold your name, new sites in your field that show your brand |

The agent hands its finds over and stops there. It contacts no one and drafts no message. The protection skill says what a domain is doing and gives no legal assessment or advice: what a domain means for you, and what you do about it, stays with you.

## How it works

1. **Set up a task.** Your AI agent reads the skill and talks the task through with you: what you want to find and what for, the keywords or TLDs, the filters, how often to pull and check, and where the finds go. The result is a `config.json` in a task folder. No site is fetched in this step.
2. **Pull.** `scripts/pull.py` asks DomainKits for the domains registered in the last `window_days` days (3 by default) that match the task's keywords or TLDs and filters, and adds the new ones to `state.csv`. A protection task also pulls the registered typos of your domain.
3. **Check.** `scripts/run.py` takes the rows that are due, reads robots.txt, fetches the homepage through the fetch service, and gives what came back to a model with section 2 of the skill. The model gives the state and what to tell you. The program writes the row and works out the time of the next check from `frequency`. Finds go to `found.csv`.
4. **Close the loop.** You read `found.csv` with your agent, say which finds were of use, and the task is adjusted in `config.json`.

## Requirements

- Python 3.9 or later
- A [DomainKits](https://domainkits.com) API key for the data. Which filters can be combined in one search depends on the account's plan.
- A [Firecrawl](https://firecrawl.dev) API key for fetching pages
- A model key: Anthropic for Claude, or OpenAI for a model written as `openai:<model>`
- An AI agent with the [DomainKits MCP](https://domainkits.com/mcp) for the set-up conversation, such as Claude Code

Keys are read from environment variables, or from a file named by `env_file` in `config.json`. `config.json` holds only the names of the variables, not the keys themselves:

```json
"keys": {"data": "DOMAINKITS_API_KEY", "fetch": "FIRECRAWL_API_KEY", "model": "ANTHROPIC_API_KEY"}
```

## Quick start

Install the packages, from the folder of this repository:

```bash
pip install -r requirements.txt
```

Connect the DomainKits MCP to your agent. In Claude Code:

```bash
claude mcp add --transport http domainkits https://api.domainkits.com/v1/mcp --header "X-API-Key: YOUR_KEY"
```

Start your agent in this folder and ask it to set up a task, for example "set up a new-sites task in tasks/flooring". It follows [SKILL.md](SKILL.md), or [protection/SKILL.md](protection/SKILL.md) for a brand, and writes the task's `config.json`. For this conversation a top-tier model is recommended (Claude Opus 5.5 or above).

Then run the two programs on the task folder:

```bash
python scripts/pull.py tasks/flooring
```

```bash
python scripts/run.py tasks/flooring
```

Start both from your own scheduler (cron, Windows Task Scheduler), for example every hour. The pull is made only when `pull_every` has passed since the last one, and the check takes only the rows that are due. The skill sets up no schedule for you.

## Models

| Role | Field in `config.json` | Recommended |
|---|---|---|
| Judge | `models.judge` | Claude Sonnet 5.5 or above, or a model of the same class of another provider. You decide. |
| Changed or not (optional) | `models.change` | A smaller model, asked first at a later check whether the site changed since its last judgement. Where it did not, the judge is not asked. |

A model name without a provider is a Claude model (`claude-sonnet-5-5`). A model of OpenAI is written `openai:<model>` and needs `"openai": "<variable name>"` under `keys`.

## The task folder

| File | Content |
|---|---|
| `config.json` | The task, written in the set-up conversation |
| `state.csv` | The state machine, one row per domain: domain, last-check, status, action, action-date |
| `domains/<domain>.jsonl` | Each domain's records: the data source row, each fetch reply as received, each model answer |
| `found.csv` | The finds for you: time, domain, status, what to tell |
| `log.jsonl` | What each run did and what it cost (fetch credits, model tokens) |
| `archive/` | Domains past `retire_days`, with their rows and files as they were |

The files of a task hold your data. `.gitignore` leaves them out of git, and `.env` files too.

## What the programs hold to

Whatever a model says:

- **robots.txt first.** robots.txt is read before each fetch, and a site it disallows is not fetched. The row says so, and you can visit the site yourself. A redirect to another host, a subdomain or another path is checked against the rules that apply there.
- **The fetcher is named.** Each request goes through Firecrawl. robots.txt is read for its token `FirecrawlAgent` and for its name `Firecrawl`, so a site can close itself to this agent under either name.
- **No direct connection.** The programs open no connection to a site. Fetching is plain: a block is not worked around.
- **Personal data.** Phone numbers, e-mail addresses and contact fields are taken out of what a model is given. A name of a person in the text of a page is not found this way. The domain's file keeps each reply as it was received.
- **Keys.** A key goes only to the service it is for: the requests to DomainKits, Firecrawl and the model APIs follow no redirect.
- **Timing.** The time of the next check is worked out by the program from `frequency`, not by the model. No site is fetched more often than `min_interval`.
- **One run at a time.** A task folder has a lock. A failure of the fetch service or of the judge's model API stops the run and says so; the row it was at is put back by a day.

The full list is in section 3 of [SKILL.md](SKILL.md).

## Exit codes

| Program | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| `pull.py` | done, or not due | did not start, the data source failed, or another start took the lock over | | |
| `run.py` | done | did not start, or another start took the lock over | the fetch service failed or did not fetch plainly | the model API failed |

"Did not start" covers a folder without `config.json`, a `config.json` the program cannot work with, a run in progress and a key that is not set. The message says which.

## Tests

The tests run offline, with temporary task folders and fakes in place of the data source, the fetch service and the models. They need no key and make no request.

```bash
python -m unittest tests.test_v0 -v
```

## Related

- [ABTdomain/domainkits-mcp](https://github.com/ABTdomain/domainkits-mcp): hosted MCP server for domain data
- [ABTdomain/domainkits-skills](https://github.com/ABTdomain/domainkits-skills): agent skills powered by DomainKits MCP
- [ABTdomain/newly-registered-domains](https://github.com/ABTdomain/newly-registered-domains): newly registered domains
- [ABTdomain/domainkits-sdk-py](https://github.com/ABTdomain/domainkits-sdk-py): Python client for the DomainKits domain data API

## License

[MIT](LICENSE)
