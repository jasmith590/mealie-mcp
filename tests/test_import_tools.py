"""
Recipe import unit tests (this fork). No live infrastructure.

- the client sends Mealie's create/url and create/html-or-json payloads as Mealie expects
- import_recipe_from_url / import_recipe_from_html: good import, empty shell, Mealie
  error, timeout, and a dict reply instead of a bare slug
- create_recipe: the from-social tag is created on first use (and only then), and
  imageUrl is best-effort (a failed picture never fails the recipe)
- set_recipe_image
- the import_recipe prompt is registered and carries the link and caption

Usage:
    python tests/test_import_tools.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcp.server.fastmcp import FastMCP

from mealie.client import MealieApiError
from mealie.recipe import RecipeMixin
from models.recipe import RecipeCreate
from prompts import register_prompts
from tools.recipe_tools import register_recipe_tools

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    results.append((name, bool(condition), detail))


def section(title: str) -> None:
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def call(srv: FastMCP, tool: str, args: dict) -> dict:
    result = asyncio.run(srv.call_tool(tool, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def call_error(srv: FastMCP, tool: str, args: dict) -> str:
    try:
        call(srv, tool, args)
    except Exception as e:
        return str(e)
    return ""


class Recording(RecipeMixin):
    """RecipeMixin with the HTTP layer replaced by a recorder."""

    def __init__(self):
        self.calls = []

    def _handle_request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        return "new-slug"


def recipe(slug, ingredients=1, instructions=1, image="img"):
    return {
        "id": "1", "slug": slug, "name": slug.replace("-", " ").title(), "image": image,
        "orgURL": f"https://example.com/{slug}",
        "recipeIngredient": [{"note": f"ing {i}"} for i in range(ingredients)],
        "recipeInstructions": [{"text": f"step {i}"} for i in range(instructions)],
        "recipeCategory": [], "tags": [], "tools": [], "settings": {}, "nutrition": {},
    }


class FakeMealie:
    """Stands in for MealieFetcher in the recipe tools."""

    def __init__(self, import_result="garlic-noodles", import_error=None, stored=None,
                 tags=(), image_error=None):
        self.import_result = import_result
        self.import_err = import_error
        self.stored = stored or {"garlic-noodles": recipe("garlic-noodles")}
        self.tags = [dict(t) for t in tags]
        self.image_error = image_error
        self.created_tags = []
        self.image_calls = []
        self.html_calls = []
        self.patches = []

    # imports
    def import_recipe_from_url(self, url, include_tags=False, include_categories=False):
        if self.import_err:
            raise self.import_err
        return self.import_result

    def import_recipe_from_html(self, data, url=None, include_tags=False, include_categories=False):
        self.html_calls.append((data, url, include_tags))
        if self.import_err:
            raise self.import_err
        return self.import_result

    # reads
    def get_recipe(self, slug):
        return json.loads(json.dumps(self.stored.get(slug) or recipe(slug)))

    def get_household_recipe(self, slug):
        return {"lastMade": None}

    def get_group_self(self):
        return {"slug": "home"}

    # create_recipe path
    def create_recipe(self, name):
        return "new-recipe"

    def get_categories(self, per_page=None):
        return {"items": []}

    def get_tags(self, per_page=None):
        return {"items": self.tags}

    def create_tag(self, name):
        tag = {"id": f"id-{name}", "name": name, "slug": name}
        self.created_tags.append(name)
        self.tags.append(tag)
        return tag

    def get_organizer_tools(self, per_page=None):
        return {"items": []}

    def get_foods(self, per_page=None):
        return {"items": [{"id": "f1", "name": "noodles"}]}

    def create_food(self, name):
        return {"id": f"f-{name}", "name": name}

    def get_organizer_units(self, per_page=None):
        return {"items": [{"id": "u1", "name": "ounce", "abbreviation": "oz"}]}

    def patch_recipe(self, slug, data):
        self.patches.append(data)
        out = recipe(slug)
        out["tags"] = data.get("tags", [])
        return out

    def scrape_recipe_image_from_url(self, slug, image_url):
        self.image_calls.append((slug, image_url))
        if self.image_error:
            raise self.image_error
        return {"success": True}


def server(fake: FakeMealie) -> FastMCP:
    srv = FastMCP("probe")
    register_recipe_tools(srv, fake)
    return srv


SOCIAL_RECIPE = {
    "name": "Garlic Butter Noodles",
    "tags": ["from-social"],
    "orgURL": "https://www.tiktok.com/@cook/video/123",
    "recipeIngredient": [{"quantity": 8, "unit": "oz", "food": "noodles"}],
    "recipeInstructions": [{"text": "Boil the noodles."}],
}


def run() -> None:
    os.environ["MEALIE_PUBLIC_URL"] = "https://mealie.example.com"

    # ------------------------------------------------------------------
    section("Client: import payloads match Mealie's schemas")
    # ------------------------------------------------------------------
    rec = Recording()
    slug = rec.import_recipe_from_url("https://site.test/r", include_tags=True)
    method, url, body = rec.calls[-1]
    check("create/url: POST to /api/recipes/create/url", (method, url) == ("POST", "/api/recipes/create/url"))
    check("create/url: camelCase body", body == {"url": "https://site.test/r", "includeTags": True,
                                                  "includeCategories": False}, str(body))
    check("create/url: returns the slug", slug == "new-slug")

    rec.import_recipe_from_html("<html>recipe</html>", url="https://site.test/r")
    method, url, body = rec.calls[-1]
    check("html-or-json: POST to /api/recipes/create/html-or-json",
          (method, url) == ("POST", "/api/recipes/create/html-or-json"))
    check("html-or-json: data, url and flags", body == {"data": "<html>recipe</html>", "url": "https://site.test/r",
                                                         "includeTags": False, "includeCategories": False}, str(body))
    rec.import_recipe_from_html('{"@type": "Recipe"}')
    check("html-or-json: url omitted when not given", "url" not in rec.calls[-1][2], str(rec.calls[-1][2]))

    for label, fn in (("url", lambda: rec.import_recipe_from_url("")),
                      ("html", lambda: rec.import_recipe_from_html("   "))):
        try:
            fn()
            check(f"empty {label} is rejected before calling Mealie", False)
        except ValueError:
            check(f"empty {label} is rejected before calling Mealie", True)

    # ------------------------------------------------------------------
    section("import_recipe_from_url")
    # ------------------------------------------------------------------
    r = call(server(FakeMealie()), "import_recipe_from_url", {"url": "https://blog.test/garlic-noodles"})
    check("good import: ok with counts", r.get("ok") is True and r.get("ingredientCount") == 1
          and r.get("instructionCount") == 1, str(r))
    check("good import: recipe link", r.get("url") == "https://mealie.example.com/g/home/r/garlic-noodles", str(r))
    check("good import: no reason", "reason" not in r, str(r))

    fake = FakeMealie(stored={"garlic-noodles": recipe("garlic-noodles", ingredients=0)})
    r = call(server(fake), "import_recipe_from_url", {"url": "https://blog.test/garlic-noodles"})
    check("empty shell: ok=false with the slug kept", r.get("ok") is False and r.get("slug") == "garlic-noodles",
          str(r))
    check("empty shell: says it was kept, not deleted", "never deletes" in r.get("reason", ""), r.get("reason", ""))

    err = MealieApiError(400, "API error", json.dumps({"detail": {"message": "No Recipe Found"}}))
    r = call(server(FakeMealie(import_error=err)), "import_recipe_from_url", {"url": "https://x.test"})
    check("Mealie 400: ok=false with Mealie's message", r.get("ok") is False and "No Recipe Found" in r.get("reason", "")
          and "400" in r.get("reason", ""), str(r))
    check("Mealie 400: keeps the source link for create_recipe", r.get("orgURL") == "https://x.test", str(r))

    r = call(server(FakeMealie(import_error=MealieApiError(500, "API error", "boom"))),
             "import_recipe_from_url", {"url": "https://x.test"})
    check("Mealie 500 with a non-JSON body: reason has the body", "boom" in r.get("reason", ""), str(r))

    r = call(server(FakeMealie(import_error=TimeoutError("slow"))), "import_recipe_from_url", {"url": "https://x.test"})
    check("timeout: says to search before retrying", r.get("ok") is False and "get_recipes" in r.get("reason", ""),
          str(r))

    r = call(server(FakeMealie(import_result={"slug": "garlic-noodles"})), "import_recipe_from_url",
             {"url": "https://blog.test/garlic-noodles"})
    check("dict reply with a slug is accepted", r.get("ok") is True and r.get("slug") == "garlic-noodles", str(r))

    r = call(server(FakeMealie(import_result={})), "import_recipe_from_url", {"url": "https://x.test"})
    check("reply without a slug: ok=false", r.get("ok") is False and "slug" in r.get("reason", ""), str(r))

    # ------------------------------------------------------------------
    section("import_recipe_from_html")
    # ------------------------------------------------------------------
    fake = FakeMealie()
    r = call(server(fake), "import_recipe_from_html",
             {"data": '{"@type": "Recipe"}', "source_url": "https://blog.test/garlic-noodles"})
    check("html import: ok", r.get("ok") is True, str(r))
    check("html import: source_url passed through", fake.html_calls[-1][1] == "https://blog.test/garlic-noodles",
          str(fake.html_calls))

    # ------------------------------------------------------------------
    section("create_recipe: from-social tag and imageUrl")
    # ------------------------------------------------------------------
    fake = FakeMealie()
    r = call(server(fake), "create_recipe", {"recipe": SOCIAL_RECIPE})
    check("missing from-social tag is created once", fake.created_tags == ["from-social"], str(fake.created_tags))
    check("from-social tag is in the saved tags",
          [t["slug"] for t in fake.patches[-1]["tags"]] == ["from-social"], str(fake.patches[-1]["tags"]))
    check("orgURL is saved", fake.patches[-1].get("orgURL") == SOCIAL_RECIPE["orgURL"])
    check("no imageUrl: no image call and no imageStatus", not fake.image_calls and "imageStatus" not in r, str(r))

    fake = FakeMealie(tags=[{"id": "t1", "name": "From Social", "slug": "from-social"}])
    call(server(fake), "create_recipe", {"recipe": SOCIAL_RECIPE})
    check("existing from-social tag is reused, not created", fake.created_tags == [], str(fake.created_tags))

    fake = FakeMealie()
    call(server(fake), "create_recipe", {"recipe": {**SOCIAL_RECIPE, "tags": ["dinner"]}})
    check("other unknown tags are still dropped, nothing created",
          fake.created_tags == [] and fake.patches[-1]["tags"] == [], str(fake.patches[-1]["tags"]))

    fake = FakeMealie()
    r = call(server(fake), "create_recipe", {"recipe": {**SOCIAL_RECIPE, "imageUrl": "https://img.test/a.jpg"}})
    check("imageUrl: Mealie fetches it after saving", fake.image_calls == [("new-recipe", "https://img.test/a.jpg")],
          str(fake.image_calls))
    check("imageUrl: imageStatus=set", r.get("imageStatus") == "set", str(r.get("imageStatus")))

    fake = FakeMealie(image_error=MealieApiError(500, "image failed", ""))
    r = call(server(fake), "create_recipe", {"recipe": {**SOCIAL_RECIPE, "image": "https://img.test/a.jpg"}})
    check("image alias accepted", fake.image_calls == [("new-recipe", "https://img.test/a.jpg")], str(fake.image_calls))
    check("failed picture: recipe still returned", r.get("slug") == "new-recipe", str(r)[:200])
    check("failed picture: imageStatus explains", str(r.get("imageStatus", "")).startswith("failed:")
          and "saved without a picture" in r.get("imageStatus", ""), str(r.get("imageStatus")))

    check("RecipeCreate: image_url alias maps to imageUrl",
          RecipeCreate.model_validate({"name": "x", "image_url": "https://i.test/x.png"}).imageUrl
          == "https://i.test/x.png")

    # ------------------------------------------------------------------
    section("set_recipe_image")
    # ------------------------------------------------------------------
    fake = FakeMealie()
    r = call(server(fake), "set_recipe_image", {"slug": "garlic-noodles", "image_url": "https://img.test/b.png"})
    check("ok with link", r.get("ok") is True and r.get("url", "").endswith("/r/garlic-noodles"), str(r))
    err = call_error(server(FakeMealie(image_error=MealieApiError(422, "bad image", ""))),
                     "set_recipe_image", {"slug": "garlic-noodles", "image_url": "https://img.test/b.png"})
    check("failure raises a tool error naming the recipe", "garlic-noodles" in err, err)

    # ------------------------------------------------------------------
    section("import_recipe prompt")
    # ------------------------------------------------------------------
    srv = FastMCP("probe")
    register_prompts(srv)
    names = [p.name for p in asyncio.run(srv.list_prompts())]
    check("import_recipe is registered alongside upstream prompts",
          "import_recipe" in names and "recipe_builder" in names, str(names))
    got = asyncio.run(srv.get_prompt("import_recipe", {"source": "https://www.tiktok.com/@cook/video/123",
                                                        "caption": "8 oz noodles"}))
    texts = [m.content.text for m in got.messages]
    check("prompt routes social links away from URL import", "Path B" in texts[0] and "tiktok.com" in texts[0])
    check("user message carries link and caption",
          "tiktok.com/@cook" in texts[-1] and "8 oz noodles" in texts[-1], texts[-1])

    # ------------------------------------------------------------------
    section("Summary")
    # ------------------------------------------------------------------
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n  {passed}/{len(results)} passed", end="")
    if failed:
        print(f"  ({failed} failed)\n\nFailed:")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}" + (f": {detail}" if detail else ""))
    else:
        print(" — all tests passed")
    print()
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    run()
