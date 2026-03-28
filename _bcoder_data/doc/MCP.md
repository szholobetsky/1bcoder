# MCP Servers — Quick Reference

Connect a server with:
```
/mcp connect <name> <command>
```

---

## Official (actively maintained)

These are published by the MCP team (Anthropic). Safe to use.

### Filesystem
Read/write files under a given directory.
```
/mcp connect fs npx -y @modelcontextprotocol/server-filesystem .
/mcp connect fs npx -y @modelcontextprotocol/server-filesystem C:\Users\yourname\projects
```

### Fetch — web page reader
Fetches a URL and returns clean text (good for docs, articles).
```
/mcp connect web uvx mcp-server-fetch
```
Then: `/mcp call web/fetch {"url": "https://example.com"}`

### Git
Read commits, diffs, branches in a local repo.
```
/mcp connect git uvx mcp-server-git
```

### Memory
Persistent key-value memory that survives across sessions (stored as a local graph).
```
/mcp connect mem npx -y @modelcontextprotocol/server-memory
```

### Sequential Thinking
Breaks complex problems into thought steps — useful as a reasoning scaffold.
```
/mcp connect think npx -y @modelcontextprotocol/server-sequential-thinking
```

### Time
Returns current time and converts between timezones.
```
/mcp connect time uvx mcp-server-time
```

---

## Archived (no longer maintained by MCP team)

Still work fine but use at your own risk. Source:
https://github.com/modelcontextprotocol/servers-archived

### SQLite
Query a local SQLite database file.
```
/mcp connect db uvx mcp-server-sqlite --db-path ./mydb.sqlite
```

### PostgreSQL
Query a Postgres database via connection string.
```
/mcp connect pg npx -y @modelcontextprotocol/server-postgres postgresql://localhost/mydb
```

### Brave Search — web search
Requires a free API key from https://brave.com/search/api/
```
set BRAVE_API_KEY=your_key_here
/mcp connect search npx -y @modelcontextprotocol/server-brave-search
```

### GitHub
Read repos, issues, PRs via the GitHub API.
Requires a personal access token from https://github.com/settings/tokens
```
set GITHUB_PERSONAL_ACCESS_TOKEN=your_token_here
/mcp connect gh npx -y @modelcontextprotocol/server-github
```

### GitLab
Same as GitHub but for GitLab.
```
set GITLAB_PERSONAL_ACCESS_TOKEN=your_token_here
/mcp connect gl npx -y @modelcontextprotocol/server-gitlab
```

### Puppeteer — browser automation
Controls a real Chromium browser. Good for scraping JS-heavy pages.
Requires Node.js and will download Chromium on first run (~200 MB).
```
/mcp connect browser npx -y @modelcontextprotocol/server-puppeteer
```

### Slack
Read and post messages to Slack workspaces.
Requires a Slack bot token and team ID.
```
set SLACK_BOT_TOKEN=xoxb-...
set SLACK_TEAM_ID=T...
/mcp connect slack npx -y @modelcontextprotocol/server-slack
```

### Redis
Get/set keys in a running Redis instance.
```
/mcp connect redis npx -y @modelcontextprotocol/server-redis
```

### Google Drive
Browse and read files from Google Drive (requires OAuth setup).
```
/mcp connect gdrive npx -y @modelcontextprotocol/server-gdrive
```

### Google Maps
Geocoding, directions, place search via Google Maps API.
Requires a Google Maps API key.
```
set GOOGLE_MAPS_API_KEY=your_key_here
/mcp connect maps npx -y @modelcontextprotocol/server-google-maps
```

---

## Notes

- `npx -y` downloads the package on first run, then uses the npm cache. No permanent install.
- `uvx` does the same for Python packages via uv (`pip install uv` to get it).
- All servers run locally — no data leaves your machine except for API-key servers
  (Brave Search, GitHub, Slack, Google Maps) which call their respective APIs.
- The directory argument in `/mcp connect fs npx ... .` limits what files the server can see.
  Use a specific path instead of `.` to restrict access.
