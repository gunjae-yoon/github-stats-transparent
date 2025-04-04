#!/usr/bin/python3

import asyncio
import os
import json
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import aiohttp
import requests

CACHE_FILE = "contribution_cache.json"

###############################################################################
# Main Classes
###############################################################################

class Queries(object):
    def __init__(self, username: str, access_token: str,
                 session: aiohttp.ClientSession, max_connections: int = 10):
        self.username = username
        self.access_token = access_token
        self.session = session
        self.semaphore = asyncio.Semaphore(max_connections)

    async def query(self, generated_query: str) -> Dict:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            async with self.semaphore:
                r = await self.session.post("https://api.github.com/graphql",
                                            headers=headers,
                                            json={"query": generated_query})
            return await r.json()
        except:
            print("aiohttp failed for GraphQL query")
            async with self.semaphore:
                r = requests.post("https://api.github.com/graphql",
                                  headers=headers,
                                  json={"query": generated_query})
                return r.json()

    async def query_rest(self, path: str, params: Optional[Dict] = None) -> Dict:
        for _ in range(60):
            headers = {"Authorization": f"token {self.access_token}"}
            if params is None:
                params = dict()
            if path.startswith("/"):
                path = path[1:]
            try:
                async with self.semaphore:
                    r = await self.session.get(f"https://api.github.com/{path}",
                                               headers=headers,
                                               params=tuple(params.items()))
                if r.status == 202:
                    print(f"A path returned 202. Retrying...")
                    await asyncio.sleep(2)
                    continue
                result = await r.json()
                if result is not None:
                    return result
            except:
                print("aiohttp failed for rest query")
                async with self.semaphore:
                    r = requests.get(f"https://api.github.com/{path}",
                                     headers=headers,
                                     params=tuple(params.items()))
                    if r.status_code == 202:
                        print(f"A path returned 202. Retrying...")
                        await asyncio.sleep(2)
                        continue
                    elif r.status_code == 200:
                        return r.json()
        print("There were too many 202s. Data for this repository will be incomplete.")
        return dict()

    @staticmethod
    def repos_overview(contrib_cursor: Optional[str] = None,
                       owned_cursor: Optional[str] = None) -> str:
        return f"""{{
  viewer {{
    login,
    name,
    repositories(
        first: 100,
        orderBy: {{field: UPDATED_AT, direction: DESC}},
        isFork: false,
        after: {"null" if owned_cursor is None else '"'+ owned_cursor +'"'}
    ) {{
      pageInfo {{hasNextPage endCursor}}
      nodes {{
        nameWithOwner
        stargazers {{totalCount}}
        forkCount
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{size node {{name color}}}}
        }}
      }}
    }}
    repositoriesContributedTo(
        first: 100,
        includeUserRepositories: false,
        orderBy: {{field: UPDATED_AT, direction: DESC}},
        contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY, PULL_REQUEST_REVIEW],
        after: {"null" if contrib_cursor is None else '"'+ contrib_cursor +'"'}
    ) {{
      pageInfo {{hasNextPage endCursor}}
      nodes {{
        nameWithOwner
        stargazers {{totalCount}}
        forkCount
        languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{
          edges {{size node {{name color}}}}
        }}
      }}
    }}
  }}
}}"""

    @staticmethod
    def contrib_years() -> str:
        return """
query {
  viewer {
    contributionsCollection {
      contributionYears
    }
  }
}
"""

    @staticmethod
    def contribs_by_year(year: str) -> str:
        return f"""
    year{year}: contributionsCollection(
        from: "{year}-01-01T00:00:00Z",
        to: "{int(year) + 1}-01-01T00:00:00Z"
    ) {{
      contributionCalendar {{
        totalContributions
      }}
    }}
"""

    @classmethod
    def all_contribs(cls, years: List[str]) -> str:
        by_years = "\n".join(map(cls.contribs_by_year, years))
        return f"""
query {{
  viewer {{
    {by_years}
  }}
}}
"""

class Stats(object):
    def __init__(self, username: str, access_token: str,
                 session: aiohttp.ClientSession,
                 exclude_repos: Optional[Set] = None,
                 exclude_langs: Optional[Set] = None,
                 consider_forked_repos: bool = False):
        self.username = username
        self._exclude_repos = set() if exclude_repos is None else exclude_repos
        self._exclude_langs = set() if exclude_langs is None else exclude_langs
        self._consider_forked_repos = consider_forked_repos
        self.queries = Queries(username, access_token, session)

        self._name = None
        self._stargazers = None
        self._forks = None
        self._total_contributions = None
        self._languages = None
        self._repos = None
        self._lines_changed = None
        self._views = None
        self._ignored_repos = None
        self._repo_language_data = {}

    def _load_cache(self) -> Dict:
        try:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"repositories": {}}

    def _save_cache(self, cache: Dict) -> None:
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=2)

    async def to_str(self) -> str:
        languages = await self.languages_proportional
        formatted_languages = "\n  - ".join([f"{k}: {v:0.4f}%" for k, v in languages.items()])
        lines_changed = await self.lines_changed
        return f"""Name: {await self.name}
Stargazers: {await self.stargazers:,}
Forks: {await self.forks:,}
All-time contributions: {await self.total_contributions:,}
Repositories with contributions: {len(await self.all_repos)}
Lines of code added: {lines_changed[0]:,}
Lines of code deleted: {lines_changed[1]:,}
Lines of code changed: {lines_changed[0] + lines_changed[1]:,}
Project page views: {await self.views:,}
Languages:
  - {formatted_languages}"""

    async def get_stats(self) -> None:
        self._stargazers = 0
        self._forks = 0
        self._languages = dict()
        self._repos = set()
        self._ignored_repos = set()
        self._repo_language_data = {}
        
        next_owned = None
        next_contrib = None
        while True:
            raw_results = await self.queries.query(
                Queries.repos_overview(owned_cursor=next_owned, contrib_cursor=next_contrib)
            )
            raw_results = raw_results if raw_results is not None else {}

            self._name = raw_results.get("data", {}).get("viewer", {}).get("name", None)
            if self._name is None:
                self._name = raw_results.get("data", {}).get("viewer", {}).get("login", "No Name")

            contrib_repos = raw_results.get("data", {}).get("viewer", {}).get("repositoriesContributedTo", {})
            owned_repos = raw_results.get("data", {}).get("viewer", {}).get("repositories", {})
            
            repos = owned_repos.get("nodes", [])
            if self._consider_forked_repos:
                repos += contrib_repos.get("nodes", [])
            else:
                for repo in contrib_repos.get("nodes", []):
                    name = repo.get("nameWithOwner")
                    if name in self._ignored_repos or name in self._exclude_repos:
                        continue
                    self._ignored_repos.add(name)

            for repo in repos:
                name = repo.get("nameWithOwner")
                if name in self._repos or name in self._exclude_repos:
                    continue
                self._repos.add(name)
                self._stargazers += repo.get("stargazers").get("totalCount", 0)
                self._forks += repo.get("forkCount", 0)

                repo_langs = {}
                total_size = 0
                for lang in repo.get("languages", {}).get("edges", []):
                    lang_name = lang.get("node", {}).get("name", "Other")
                    size = lang.get("size", 0)
                    if lang_name in self._exclude_langs:
                        continue
                    repo_langs[lang_name] = size
                    total_size += size
                
                if total_size > 0:
                    self._repo_language_data[name] = {lang: size / total_size for lang, size in repo_langs.items()}

                for lang in repo.get("languages", {}).get("edges", []):
                    lang_name = lang.get("node", {}).get("name", "Other")
                    if lang_name in self._exclude_langs:
                        continue
                    if lang_name in self._languages:
                        self._languages[lang_name]["size"] += lang.get("size", 0)
                        self._languages[lang_name]["occurrences"] += 1
                    else:
                        self._languages[lang_name] = {
                            "size": lang.get("size", 0),
                            "occurrences": 1,
                            "color": lang.get("node", {}).get("color")
                        }

            if owned_repos.get("pageInfo", {}).get("hasNextPage", False) or \
                    contrib_repos.get("pageInfo", {}).get("hasNextPage", False):
                next_owned = owned_repos.get("pageInfo", {}).get("endCursor", next_owned)
                next_contrib = contrib_repos.get("pageInfo", {}).get("endCursor", next_contrib)
            else:
                break

        langs_total = sum([v.get("size", 0) for v in self._languages.values()])
        for k, v in self._languages.items():
            v["prop"] = 100 * (v.get("size", 0) / langs_total)

    @property
    async def name(self) -> str:
        if self._name is not None:
            return self._name
        await self.get_stats()
        assert(self._name is not None)
        return self._name

    @property
    async def stargazers(self) -> int:
        if self._stargazers is not None:
            return self._stargazers
        await self.get_stats()
        assert(self._stargazers is not None)
        return self._stargazers

    @property
    async def forks(self) -> int:
        if self._forks is not None:
            return self._forks
        await self.get_stats()
        assert(self._forks is not None)
        return self._forks

    @property
    async def languages(self) -> Dict:
        if self._languages is not None:
            return self._languages
        await self.get_stats()
        assert(self._languages is not None)
        return self._languages

    @property
    async def languages_proportional(self) -> Dict:
        if self._languages is None:
            await self.get_stats()
            assert(self._languages is not None)
        return {k: v.get("prop", 0) for (k, v) in self._languages.items()}

    @property
    async def repos(self) -> List[str]:
        if self._repos is not None:
            return self._repos
        await self.get_stats()
        assert(self._repos is not None)
        return self._repos
    
    @property
    async def all_repos(self) -> List[str]:
        if self._repos is not None and self._ignored_repos is not None:
            return self._repos | self._ignored_repos
        await self.get_stats()
        assert(self._repos is not None)
        assert(self._ignored_repos is not None)
        return self._repos | self._ignored_repos

    @property
    async def total_contributions(self) -> int:
        if self._total_contributions is not None:
            return self._total_contributions
        self._total_contributions = 0
        years = (await self.queries.query(Queries.contrib_years())) \
            .get("data", {}).get("viewer", {}).get("contributionsCollection", {}).get("contributionYears", [])
        by_year = (await self.queries.query(Queries.all_contribs(years))) \
            .get("data", {}).get("viewer", {}).values()
        for year in by_year:
            self._total_contributions += year.get("contributionCalendar", {}).get("totalContributions", 0)
        return self._total_contributions

    @property
    async def lines_changed(self) -> Tuple[int, int]:
        """
        :return: count of total lines added, removed, or modified by the user
        """
        if self._lines_changed is not None:
            return self._lines_changed

        additions = 0
        deletions = 0
        cache = self._load_cache()
        updated_cache = cache["repositories"].copy()

        await self.get_stats()

        for repo in await self.all_repos:
            r = await self.queries.query_rest(f"/repos/{repo}/stats/contributors")
            repo_additions = 0
            repo_deletions = 0
            success = False

            if isinstance(r, list):
                for author_obj in r:
                    if not isinstance(author_obj, dict) or not isinstance(author_obj.get("author", {}), dict):
                        continue
                    author = author_obj.get("author", {}).get("login", "")
                    if author != self.username:
                        continue
                    for week in author_obj.get("weeks", []):
                        repo_additions += week.get("a", 0)
                        repo_deletions += week.get("d", 0)
                success = True
            else:
                print(f"Failed to fetch contributions for {repo}. Using cache if available.")

            if success:
                lang_contribs = {}
                if repo in self._repo_language_data:
                    for lang, prop in self._repo_language_data[repo].items():
                        lang_contribs[lang] = {
                            "additions": int(repo_additions * prop),
                            "deletions": int(repo_deletions * prop)
                        }
                else:
                    lang_contribs["Unknown"] = {
                        "additions": repo_additions,
                        "deletions": repo_deletions
                    }

                updated_cache[repo] = {
                    "additions": repo_additions,
                    "deletions": repo_deletions,
                    "last_updated": datetime.utcnow().isoformat() + "Z",
                    "languages": lang_contribs
                }
                additions += repo_additions
                deletions += repo_deletions
            elif repo in cache["repositories"]:
                cached_data = cache["repositories"][repo]
                additions += cached_data["additions"]
                deletions += cached_data["deletions"]
                updated_cache[repo] = cached_data
                print(f"Using cached data for {repo}")
            else:
                print(f"No cache available for {repo}. Skipping.")

        cache["repositories"] = updated_cache
        self._save_cache(cache)

        self._lines_changed = (additions, deletions)
        return self._lines_changed

    @property
    async def views(self) -> int:
        if self._views is not None:
            return self._views
        total = 0
        for repo in await self.repos:
            r = await self.queries.query_rest(f"/repos/{repo}/traffic/views")
            for view in r.get("views", []):
                total += view.get("count", 0)
        self._views = total
        return total

###############################################################################
# Main Function
###############################################################################

async def main() -> None:
    access_token = os.getenv("ACCESS_TOKEN")
    user = os.getenv("GITHUB_ACTOR")
    async with aiohttp.ClientSession() as session:
        s = Stats(user, access_token, session)
        print(await s.to_str())

if __name__ == "__main__":
    asyncio.run(main())