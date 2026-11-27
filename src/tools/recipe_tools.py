import json
import logging
import os
import traceback
import uuid
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mealie import MealieFetcher
from mealie.client import MealieApiError
from models.recipe import RecipeCreate
from recipe_times import add_readable_times
from social import fetch_caption
from trim import trim_recipe_concise, trim_recipe_detail, trim_recipe_summary

logger = logging.getLogger("mealie-mcp")


NO_PUBLIC_URL = (
    "No link: MEALIE_PUBLIC_URL (the address people open Mealie at) isn't set on the "
    "MCP server"
)


# Tag put on recipes built from social media posts. Unlike other tags, create_recipe
# creates it on first use instead of dropping it.
SOCIAL_TAG = "from-social"

EMPTY_IMPORT = (
    "Mealie created a recipe from this source but found no ingredients or no "
    "instructions in it. It was kept. Build the recipe with create_recipe instead, "
    "then try delete_empty_recipe on this slug; if it refuses (the import kept a "
    "description or some ingredients), tell the user it can be deleted in Mealie."
)


def import_error(e: Exception) -> str:
    """A short reason for a failed import, from Mealie's error body when it has one."""
    if isinstance(e, TimeoutError):
        return (
            "Mealie took too long to import this page. It may still finish in the "
            "background, so search with get_recipes before trying again."
        )
    if isinstance(e, MealieApiError):
        message = None
        try:
            detail = json.loads(e.response_text or "").get("detail")
            if isinstance(detail, dict):
                message = detail.get("message") or detail.get("error")
            elif isinstance(detail, str):
                message = detail
        except (ValueError, AttributeError):
            pass
        return f"Mealie could not import it ({e.status_code}): {message or e.response_text or e.message}"
    return f"Import failed: {e}"


def register_recipe_tools(mcp: FastMCP, mealie: MealieFetcher) -> None:
    """Register all recipe-related tools with the MCP server."""

    group: Dict[str, str] = {}

    def web_link(path: str) -> Optional[str]:
        """A link into Mealie's web app, e.g. web_link("r/<slug>"), or None when
        MEALIE_PUBLIC_URL isn't set: MEALIE_BASE_URL is usually an internal Docker
        address no one can open, and Mealie's API doesn't say its public address."""
        public_url = os.getenv("MEALIE_PUBLIC_URL", "").strip().rstrip("/")
        if not public_url:
            return None
        if "slug" not in group:
            group["slug"] = mealie.get_group_self()["slug"]
        return f"{public_url}/g/{group['slug']}/{path}"

    def detailed(slug: str, recipe: Dict[str, Any]) -> Dict[str, Any]:
        """A whole recipe as get_recipe_detailed returns it: readable times, lastMade,
        trimmed, and the recipe page url. create_recipe and duplicate_recipe return the
        same, so a recipe looks the same whichever tool returns it."""
        out = trim_recipe_detail(add_last_made(slug, add_readable_times(recipe)))
        url = web_link(f"r/{out.get('slug', slug)}")
        if url:
            out["url"] = url
        return out

    def share_link(token: Dict[str, Any], recipe: Dict[str, Any]) -> Dict[str, Any]:
        """A share token as the link to send: id, recipeName, recipeSlug, expiresAt, url."""
        out = {"id": token.get("id"), "recipeName": recipe.get("name"), "recipeSlug": recipe.get("slug"),
               "expiresAt": token.get("expiresAt")}
        url = web_link(f"shared/r/{token.get('id')}")
        if url:
            out["url"] = url
        else:
            out["urlNote"] = NO_PUBLIC_URL
        return out

    def add_last_made(slug: str, recipe: Dict[str, Any]) -> Dict[str, Any]:
        """Set recipe["lastMade"] from the household's recipe data, where Mealie v3.29+
        keeps it. A failed lookup is logged and skipped rather than failing the read."""
        try:
            recipe["lastMade"] = mealie.get_household_recipe(slug).get("lastMade")
        except Exception as e:
            logger.warning({"message": "Could not fetch lastMade", "slug": slug, "error": str(e)})
        return recipe

    def import_summary(created: Any) -> Dict[str, Any]:
        """What an import produced, so the assistant can tell a good scrape from an
        empty shell without reading the whole recipe."""
        slug = created if isinstance(created, str) else (created or {}).get("slug")
        if not slug:
            return {"ok": False, "reason": f"Mealie did not return a recipe slug: {created!r}"}
        recipe = mealie.get_recipe(slug)
        ingredients = recipe.get("recipeIngredient") or []
        instructions = recipe.get("recipeInstructions") or []
        out: Dict[str, Any] = {
            "ok": bool(ingredients) and bool(instructions),
            "slug": slug,
            "name": recipe.get("name"),
            "ingredientCount": len(ingredients),
            "instructionCount": len(instructions),
            "hasImage": bool(recipe.get("image")),
            "orgURL": recipe.get("orgURL"),
        }
        try:
            url = web_link(f"r/{slug}")
        except Exception as e:
            logger.warning({"message": "Could not build recipe link", "slug": slug, "error": str(e)})
            url = None
        if url:
            out["url"] = url
        if not out["ok"]:
            out["reason"] = EMPTY_IMPORT
        return out

    @mcp.tool()
    def get_recipes(
        search: Optional[str] = None,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        categories: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        require_all_tags: Optional[bool] = None,
        require_all_categories: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Provides a paginated list of recipes with optional filtering.

        IMPORTANT: When filtering by tags or categories, you MUST use slugs or UUIDs, NOT display names!
        - ✅ Correct: tags=["quick-meals", "vegetarian"]
        - ❌ Wrong: tags=["Quick Meals", "Vegetarian"]

        Use get_tags() or get_categories() first to find the correct slugs.

        Args:
            search: Filters recipes by name or description.
            page: Page number for pagination.
            per_page: Number of items per page.
            categories: Filter by category SLUGS (e.g., ["breakfast", "dinner"]).
            tags: Filter by tag SLUGS or UUIDs (e.g., ["quick", "healthy"]).
            require_all_tags: If True, recipe must have ALL specified tags (AND). Default False (OR).
            require_all_categories: If True, recipe must have ALL specified categories (AND).

        Returns:
            Dict[str, Any]: Pagination fields and items, one short entry per recipe: id
                (use it as recipe_id in meal plans and shopping lists), name, slug,
                description, tags / recipeCategory / tools as slugs, prepTime/performTime/
                totalTime as readable text ("1 hour 30 minutes") plus *TimeSeconds as
                numbers for comparisons, recipeServings, recipeYieldQuantity + recipeYield,
                rating (if set), dateAdded, dateUpdated. Ingredients and instructions are
                not included: use get_recipe_concise (ingredients) or get_recipe_detailed
                (everything) for a chosen recipe.
        """
        try:
            logger.info(
                {
                    "message": "Fetching recipes",
                    "search": search,
                    "page": page,
                    "per_page": per_page,
                    "categories": categories,
                    "tags": tags,
                    "require_all_tags": require_all_tags,
                    "require_all_categories": require_all_categories,
                }
            )
            result = mealie.get_recipes(
                search=search,
                page=page,
                per_page=per_page,
                categories=categories,
                tags=tags,
                require_all_tags=require_all_tags,
                require_all_categories=require_all_categories,
            )
            result["items"] = [
                trim_recipe_summary(add_readable_times(item)) for item in result.get("items", [])
            ]
            return result
        except Exception as e:
            error_msg = f"Error fetching recipes: {str(e)}"
            logger.error({"message": error_msg})
            logger.debug(
                {"message": "Error traceback", "traceback": traceback.format_exc()}
            )
            raise ToolError(error_msg)

    @mcp.tool()
    def get_recipe_detailed(slug: str) -> Dict[str, Any]:
        """Retrieve a specific recipe by its slug identifier. Use this to get full recipe
        details for tasks like updating or displaying the recipe.

        Args:
            slug: The unique text identifier for the recipe, typically found in recipe URLs
                or from get_recipes results.

        Returns:
            Dict[str, Any]: The whole recipe: ingredients, instructions, nutrition, notes,
                description, servings and yield, rating, orgURL (where it was imported
                from), dates, and tags / recipeCategory / tools as slugs. Each ingredient
                gives food, unit and label (store section) as names, plus quantity, note,
                display (the full line as text) and referenceId (instruction steps link to
                ingredients by it). prepTime/performTime/totalTime are readable text; the
                *TimeSeconds fields hold the same times as numbers. lastMade is the
                household's last made date (null if never made). url is the recipe's
                page in Mealie (to open, edit, or print / save as PDF) — only for
                someone logged in to Mealie; use create_recipe_share_token to share it.
        """
        try:
            logger.info({"message": "Fetching recipe", "slug": slug})
            return detailed(slug, mealie.get_recipe(slug))
        except Exception as e:
            error_msg = f"Error fetching recipe with slug '{slug}': {str(e)}"
            logger.error({"message": error_msg})
            logger.debug(
                {"message": "Error traceback", "traceback": traceback.format_exc()}
            )
            raise ToolError(error_msg)

    @mcp.tool()
    def get_recipe_concise(slug: str) -> Dict[str, Any]:
        """Retrieve a concise version of a specific recipe by its slug identifier. Use this when you only
        need a summary of the recipe, such as for when mealplaning.

        Args:
            slug: The unique text identifier for the recipe, typically found in recipe URLs
                or from get_recipes results.

        Returns:
            Dict[str, Any]: name, slug, recipeServings, recipeYieldQuantity + recipeYield,
                totalTime, rating and lastMade (when set), and recipeIngredient: each
                ingredient's food, unit and label (store section) as names, plus quantity,
                note and display (the full line as text).
        """
        try:
            logger.info({"message": "Fetching recipe", "slug": slug})
            recipe_json = add_last_made(slug, add_readable_times(mealie.get_recipe(slug)))
            return trim_recipe_concise(recipe_json)
        except Exception as e:
            error_msg = f"Error fetching recipe with slug '{slug}': {str(e)}"
            logger.error({"message": error_msg})
            logger.debug(
                {"message": "Error traceback", "traceback": traceback.format_exc()}
            )
            raise ToolError(error_msg)

    @mcp.tool()
    def get_recipe_comments(slug: str) -> List[Dict[str, Any]]:
        """Get all comments on a recipe.

        Args:
            slug: The unique text identifier for the recipe

        Returns:
            List[Dict[str, Any]]: List of comments with text, author, and timestamps
        """
        try:
            logger.info({"message": "Fetching recipe comments", "slug": slug})
            return mealie.get_recipe_comments(slug)
        except Exception as e:
            error_msg = f"Error fetching comments for recipe '{slug}': {str(e)}"
            logger.error({"message": error_msg})
            logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
            raise ToolError(error_msg)

    @mcp.tool()
    def create_recipe_comment(slug: str, text: str) -> Dict[str, Any]:
        """Post a comment on a recipe. Use this to record cooking notes such as
        substitutions made, timing adjustments, or how the dish turned out.

        Args:
            slug: The unique text identifier for the recipe
            text: The comment text

        Returns:
            Dict[str, Any]: The created comment
        """
        try:
            logger.info({"message": "Creating recipe comment", "slug": slug})
            return mealie.create_recipe_comment(slug, text)
        except Exception as e:
            error_msg = f"Error creating comment for recipe '{slug}': {str(e)}"
            logger.error({"message": error_msg})
            logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
            raise ToolError(error_msg)

    @mcp.tool()
    def get_recipe_timeline(
        page: Optional[int] = None,
        per_page: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Get a chronological activity feed across all recipes. Use this to answer
        questions like 'what did I cook last Wednesday?' or 'what have I been making lately?'

        Args:
            page: Page number to retrieve
            per_page: Number of items per page

        Returns:
            Dict[str, Any]: Timeline events with recipe names and timestamps
        """
        try:
            logger.info({"message": "Fetching recipe timeline"})
            return mealie.get_recipe_timeline(page=page, per_page=per_page)
        except Exception as e:
            error_msg = f"Error fetching recipe timeline: {str(e)}"
            logger.error({"message": error_msg})
            logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
            raise ToolError(error_msg)

    @mcp.tool()
    def get_recipe_share_tokens(slug: str) -> List[Dict[str, Any]]:
        """List existing public share links for a recipe.

        Args:
            slug: The unique text identifier for the recipe

        Returns:
            List[Dict[str, Any]]: Each link's id, recipeName, recipeSlug, expiresAt and
                url (anyone with it can view the recipe, no login, until it expires)
        """
        try:
            logger.info({"message": "Fetching recipe share tokens", "slug": slug})
            recipe = mealie.get_recipe(slug)
            return [share_link(t, recipe) for t in mealie.get_recipe_share_tokens(slug)]
        except Exception as e:
            error_msg = f"Error fetching share tokens for recipe '{slug}': {str(e)}"
            logger.error({"message": error_msg})
            logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
            raise ToolError(error_msg)

    @mcp.tool()
    def create_recipe_share_token(slug: str) -> Dict[str, Any]:
        """Create a public share link for a recipe. Use this to share a recipe
        with someone outside Mealie (e.g., text it to a family member).

        Args:
            slug: The unique text identifier for the recipe

        Returns:
            Dict[str, Any]: The new link's id, recipeName, recipeSlug, expiresAt (Mealie's
                default: 30 days) and url — anyone with the url can view the recipe,
                no login, until it expires
        """
        try:
            logger.info({"message": "Creating recipe share token", "slug": slug})
            recipe = mealie.get_recipe(slug)
            return share_link(mealie.create_recipe_share_token(slug), recipe)
        except Exception as e:
            error_msg = f"Error creating share token for recipe '{slug}': {str(e)}"
            logger.error({"message": error_msg})
            logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
            raise ToolError(error_msg)

    @mcp.tool()
    def create_recipe(recipe: RecipeCreate) -> Dict[str, Any]:
        """Create a new recipe in Mealie.

        Before calling this tool:
        1. Fetch 2-3 existing similar recipes with get_recipe_detailed to observe the
           category/tag/tool patterns used — follow those patterns, don't invent new ones.
        2. For every ingredient food, search the catalog with get_foods first. Pick the
           existing food that best covers the ingredient (e.g. "black pepper" covers
           "pepper"). Only use a name that isn't in the catalog if the food is truly
           absent — the tool will create it automatically.
        3. For every ingredient unit, call get_units first and pick the best match
           (e.g. "c" or "cup" → use the existing "cup" unit) so the unit is stored
           structurally. A unit with no catalog match is not dropped — it is appended
           to the ingredient's note so the text is preserved — but prefer matching an
           existing unit, since a note-only unit does not participate in scaling.
        4. Tags, categories and tools must already exist (check get_tags /
           get_categories / get_cooking_tools). There is no default tag. Unknown ones
           are left off the recipe and listed in the reply under droppedTags,
           droppedCategories or droppedTools; tell the user about any that were dropped.
        5. Servings and yield — set only what fits the recipe (Mealie shows them separately):
           recipeServings is the number of people served (e.g. 4) — use it for most dishes.
           recipeYieldQuantity + recipeYield describe a produced amount: recipeYieldQuantity is
           the number (e.g. 12) and recipeYield is the BARE unit only ("cookies", "cups", "loaf"),
           shown together as "12 cookies". Never put a per-serving breakdown or sentence in
           recipeYield. Most people-fed dishes use recipeServings only and leave yield blank; set
           yield only when the output is a countable/measurable thing worth stating; set both only
           when the yield differs from portioning (e.g. yields 12 meatballs but serves 4).
        6. Time: use human-readable strings WITH a unit — e.g. "30 minutes", "1 hour",
           "1 hour 30 minutes". A bare number like "45" is rejected (minutes or hours?).
           Alternatively pass prepTimeSeconds/performTimeSeconds/totalTimeSeconds as numbers
           (e.g. copied from get_recipe_detailed). Times are stored as Mealie's structured
           durations; a non-duration such as "overnight" is kept as text. Omit a time that
           doesn't apply (e.g. no cook time) rather than sending "0 minutes".
        7. Nutrition: always populate every nutrition field (calories, carbohydrateContent,
           cholesterolContent, fatContent, fiberContent, proteinContent, saturatedFatContent,
           sodiumContent, sugarContent, transFatContent, unsaturatedFatContent). Enter
           per-serving values — Mealie stores nutrition statically and never rescales it.
           Estimate from standard sources if exact values are unknown. All values are strings.

        8. Source and picture: set orgURL to the link the recipe came from. Set imageUrl
           to a direct image link if you have one; it is added after the recipe is saved
           and a failure only shows in imageStatus, never fails the recipe.
        9. For a recipe from a social media post (TikTok, Instagram, Facebook, YouTube),
           include the "from-social" tag. It is created if it doesn't exist yet (the only
           tag this tool creates).

        The tool resolves all slug and name references to IDs automatically before saving.
        When nutrition data is provided, showNutrition is enabled automatically.

        Use these EXACT field names (unknown fields are rejected, not ignored):
        recipeCategory, tags, tools, recipeIngredient, recipeInstructions, prepTime,
        performTime (the cook/active time), totalTime, prepTimeSeconds,
        performTimeSeconds, totalTimeSeconds, recipeServings,
        recipeYieldQuantity, recipeYield, orgURL, nutrition, notes, imageUrl. Worked example:

            {
              "name": "Quick Tomato Sauce",
              "recipeCategory": ["sauce"],
              "tags": ["quick"],
              "recipeServings": 4,
              "recipeYieldQuantity": 2, "recipeYield": "cups",
              "prepTime": "10 minutes", "performTime": "20 minutes", "totalTime": "30 minutes",
              "recipeIngredient": [
                {"quantity": 2, "unit": "cup", "food": "tomato", "note": "chopped"}
              ],
              "recipeInstructions": [{"text": "Simmer until thick."}],
              "nutrition": {"calories": "90", "carbohydrateContent": "12"}
            }

        Args:
            recipe: RecipeCreate object with name, description, categories (slugs),
                tags (slugs), tools (slugs), ingredients (food/unit names), instructions,
                timing, servings, yield, source URL, nutrition, and notes.

        Returns:
            Dict[str, Any]: The created recipe, as get_recipe_detailed returns it
        """
        try:
            logger.info({"message": "Creating recipe", "name": recipe.name})

            # Everything is resolved before the recipe exists. Mealie creates a recipe
            # in two calls (POST a named skeleton, then PATCH the rest), so a failure
            # between them used to leave an empty recipe behind, and every retry added
            # another one with a "(1)", "(2)" suffix.

            # Step 1: Resolve categories, tags, tools → full objects.
            # Match by slug OR name, case-insensitively, so a value the LLM sends as a
            # display name or wrong case ("Sauce" vs "sauce") resolves instead of being
            # silently dropped. Any genuinely-unmatched value is logged, not swallowed.
            dropped: Dict[str, List[str]] = {}

            def resolve_organizer(
                items: List[str], fetch_fn, kind: str = "organizer"
            ) -> List[Dict[str, Any]]:
                if not items:
                    return []
                all_items = fetch_fn(per_page=200).get("items", [])
                lookup: Dict[str, Any] = {}
                for i in all_items:
                    if i.get("slug"):
                        lookup[str(i["slug"]).lower()] = i
                    if i.get("name"):
                        lookup[str(i["name"]).lower()] = i
                resolved: List[Dict[str, Any]] = []
                for s in items:
                    obj = lookup.get(str(s).lower())
                    if obj is not None:
                        resolved.append(obj)
                    else:
                        logger.info({"message": f"{kind} not matched — dropped", "value": s})
                        dropped.setdefault(kind, []).append(s)
                return resolved

            resolved_categories = resolve_organizer(recipe.recipeCategory, mealie.get_categories, "category")
            resolved_tags = resolve_organizer(recipe.tags, mealie.get_tags, "tag")
            wants_social = any(str(t).lower() == SOCIAL_TAG for t in recipe.tags)
            has_social = any(str(t.get("slug", "")).lower() == SOCIAL_TAG for t in resolved_tags)
            if wants_social and not has_social:
                try:
                    resolved_tags.append(mealie.create_tag(SOCIAL_TAG))
                    logger.info({"message": "Created tag", "name": SOCIAL_TAG})
                    dropped["tag"] = [t for t in dropped.get("tag", []) if str(t).lower() != SOCIAL_TAG]
                except Exception as e:
                    logger.warning({"message": "Could not create tag", "name": SOCIAL_TAG, "error": str(e)})
            resolved_tools = resolve_organizer(recipe.tools, mealie.get_organizer_tools, "tool")

            # Step 2: Resolve ingredient food names and unit names → full objects.
            # Foods are looked up one name at a time with Mealie's search, never from
            # one big page: Mealie seeds thousands of foods, so a single page missed
            # most of them, and the "missing" food was re-created, which Mealie
            # refused as a duplicate. A name matches a food's name, plural name or
            # any alias, case-insensitively.
            food_cache: Dict[str, Dict[str, Any]] = {}

            def food_names(f: Dict[str, Any]) -> List[str]:
                names = [f.get("name"), f.get("pluralName")]
                names += [a.get("name") for a in f.get("aliases") or [] if isinstance(a, dict)]
                return [str(n).strip().lower() for n in names if n]

            def search_food(name: str) -> Optional[Dict[str, Any]]:
                key = name.strip().lower()
                terms = [name.strip()]
                # Mealie's search may not find "sweet potato" from "sweet potatoes";
                # try the obvious singular too, then match exactly on names.
                for suffix in ("es", "s"):
                    if key.endswith(suffix) and len(key) > len(suffix) + 2:
                        terms.append(name.strip()[: -len(suffix)])
                for term in terms:
                    items = mealie.get_foods(search=term, per_page=100).get("items", [])
                    for f in items:
                        if key in food_names(f):
                            return f
                return None

            def as_food_id(value: str) -> Optional[str]:
                try:
                    return str(uuid.UUID(value.strip()))
                except ValueError:
                    return None

            def resolve_food(name: str) -> Dict[str, Any]:
                key = name.strip().lower()
                if key in food_cache:
                    return food_cache[key]
                # A food id is looked up, never saved as a new food's name.
                food_id = as_food_id(name)
                if food_id:
                    try:
                        food = mealie.get_food(food_id)
                    except MealieApiError as e:
                        if e.status_code == 404:
                            raise ValueError(f"No food with id {food_id}; pass the food's name instead")
                        raise
                    food_cache[key] = food
                    return food
                food = search_food(name)
                if food is None:
                    try:
                        food = mealie.create_food(name.strip())
                        logger.info({"message": "Auto-created food", "name": name})
                    except MealieApiError as e:
                        # Lost a race, or the search missed it: Mealie says it exists,
                        # so use that one instead of failing the whole recipe.
                        if e.status_code != 409:
                            raise
                        food = search_food(name)
                        if food is None:
                            raise
                for n in food_names(food) + [key]:
                    food_cache[n] = food
                return food

            all_units = mealie.get_organizer_units(per_page=200).get("items", [])
            unit_lookup = {u["name"].lower(): u for u in all_units}
            unit_lookup.update({u.get("abbreviation", "").lower(): u for u in all_units if u.get("abbreviation")})

            resolved_ingredients = []
            for ing in recipe.recipeIngredient:
                # Mealie stores an ingredient sent without a referenceId with none,
                # then makes up a new random one on every read. Send one, as Mealie's
                # own editor does, so the id is stored and stays the same.
                resolved: Dict[str, Any] = {"referenceId": str(uuid.uuid4())}
                if ing.title:
                    resolved["title"] = ing.title
                if ing.quantity is not None:
                    resolved["quantity"] = ing.quantity
                if ing.note:
                    resolved["note"] = ing.note
                resolved["disableAmount"] = ing.disableAmount
                if ing.food:
                    resolved["food"] = resolve_food(ing.food)
                if ing.unit:
                    unit_obj = unit_lookup.get(ing.unit.lower())
                    if unit_obj:
                        resolved["unit"] = unit_obj
                    else:
                        # Mealie requires a unit id on PATCH, so an unresolvable
                        # unit can't go in the structured unit field. Preserve the
                        # text by appending it to the note rather than dropping it,
                        # and log it so it can be corrected on review.
                        existing_note = resolved.get("note")
                        resolved["note"] = (
                            f"{existing_note}, {ing.unit}" if existing_note else ing.unit
                        )
                        logger.info(
                            {
                                "message": "Unknown unit preserved in note",
                                "unit": ing.unit,
                                "food": ing.food,
                            }
                        )
                resolved_ingredients.append(resolved)

            # Step 3: Build patch payload
            patch_data: Dict[str, Any] = {
                "recipeCategory": resolved_categories,
                "tags": resolved_tags,
                "tools": resolved_tools,
                "recipeIngredient": resolved_ingredients,
                "recipeInstructions": [
                    {"title": i.title or "", "text": i.text, "summary": "", "ingredientReferences": []}
                    for i in recipe.recipeInstructions
                ],
            }
            if recipe.description:
                patch_data["description"] = recipe.description
            patch_data.update(recipe.time_patch())
            if recipe.recipeServings:
                patch_data["recipeServings"] = recipe.recipeServings
            if recipe.recipeYieldQuantity:
                patch_data["recipeYieldQuantity"] = recipe.recipeYieldQuantity
            if recipe.recipeYield:
                patch_data["recipeYield"] = recipe.recipeYield
            if recipe.orgURL:
                patch_data["orgURL"] = recipe.orgURL
            if recipe.nutrition:
                patch_data["nutrition"] = recipe.nutrition.model_dump(exclude_none=True)
                patch_data["settings"] = {"showNutrition": True}
            if recipe.notes:
                patch_data["notes"] = recipe.notes

            # Step 4: create the recipe, then fill it in. If the fill fails, remove
            # the skeleton this call just made so no empty recipe is left behind.
            slug = mealie.create_recipe(recipe.name)
            if not isinstance(slug, str):
                slug = slug.get("slug", slug)
            try:
                saved = mealie.patch_recipe(slug, patch_data)
            except Exception:
                try:
                    mealie.delete_empty_recipe(slug)
                    logger.info({"message": "Removed empty recipe after failed save", "slug": slug})
                except Exception as cleanup_error:
                    logger.warning(
                        {"message": "Could not remove empty recipe", "slug": slug, "error": str(cleanup_error)}
                    )
                raise
            result = detailed(slug, saved)
            for kind, key in (("tag", "droppedTags"), ("category", "droppedCategories"), ("tool", "droppedTools")):
                if dropped.get(kind):
                    result[key] = dropped[kind]

            # Step 5: picture, best-effort: the recipe is already saved
            if recipe.imageUrl:
                try:
                    mealie.scrape_recipe_image_from_url(slug, recipe.imageUrl)
                    result["imageStatus"] = "set"
                except Exception as e:
                    logger.warning({"message": "Could not set recipe image", "slug": slug, "error": str(e)})
                    result["imageStatus"] = f"failed: {e}. The recipe was saved without a picture."
            return result
        except Exception as e:
            error_msg = f"Error creating recipe '{recipe.name}': {str(e)}"
            logger.error({"message": error_msg})
            logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
            raise ToolError(error_msg)

    @mcp.tool()
    def get_social_caption(url: str) -> Dict[str, Any]:
        """Read the caption of a TikTok, Instagram, Facebook or YouTube post, without
        the video. Use it when the user shares a social media recipe link and didn't
        paste the caption themselves.

        Build the recipe from the caption with create_recipe: orgURL set to the link,
        the creator in a "Credit" note, and the thumbnail as imageUrl. If the caption
        has no real recipe (just "recipe in bio" or a dish name), say so and ask the
        user for the recipe text or a screenshot instead of inventing one.

        Args:
            url: The post link, as shared from the app

        Returns:
            Dict with ok, platform, caption (trailing hashtags removed), creator,
            creatorHandle, thumbnail and url; a reason when ok is false
        """
        logger.info({"message": "Reading social caption", "url": url})
        return fetch_caption(url)

    @mcp.tool()
    def import_recipe_from_url(url: str, include_tags: bool = False) -> Dict[str, Any]:
        """Import a recipe from a web page: Mealie fetches the page and reads its recipe.

        Use this first for ordinary recipe websites and blogs. Don't use it for social
        media posts (TikTok, Instagram, Facebook, YouTube): those pages have no recipe
        markup, so read the post with get_social_caption and build the recipe with
        create_recipe.

        Check the reply: ok=false means the import failed or came back without
        ingredients or instructions. Then build the recipe yourself with create_recipe
        (with orgURL set to this url) from what the user gave you, or ask them to paste
        the recipe text.

        Args:
            url: The recipe page link
            include_tags: Keep the page's keywords as tags (default false: pages tend
                to carry many noisy keywords)

        Returns:
            Dict with ok, slug, name, ingredientCount, instructionCount, hasImage,
            orgURL, url (the recipe in Mealie, when MEALIE_PUBLIC_URL is set), and a
            reason when ok is false
        """
        logger.info({"message": "Importing recipe from URL", "url": url})
        try:
            created = mealie.import_recipe_from_url(url, include_tags=include_tags)
        except Exception as e:
            logger.warning({"message": "URL import failed", "url": url, "error": str(e)})
            return {"ok": False, "orgURL": url, "reason": import_error(e)}
        try:
            return import_summary(created)
        except Exception as e:
            error_msg = f"Imported '{url}' but could not read the result: {e}"
            logger.error({"message": error_msg})
            logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
            raise ToolError(error_msg)

    @mcp.tool()
    def import_recipe_from_html(
        data: str, source_url: Optional[str] = None, include_tags: bool = False
    ) -> Dict[str, Any]:
        """Import a recipe from page HTML or a schema.org Recipe JSON object that you
        already have, e.g. when import_recipe_from_url failed because the site blocks
        Mealie but the page content is available to you.

        Only for HTML or JSON-LD that contains recipe markup. For plain text (a caption,
        a screenshot transcription), use create_recipe instead.

        Args:
            data: The page HTML, or a JSON string of a https://schema.org/Recipe object
            source_url: Where it came from; stored as the recipe's source link
            include_tags: Keep keywords from the markup as tags (default false)

        Returns:
            Same shape as import_recipe_from_url
        """
        logger.info({"message": "Importing recipe from HTML/JSON", "url": source_url})
        try:
            created = mealie.import_recipe_from_html(data, url=source_url, include_tags=include_tags)
        except Exception as e:
            logger.warning({"message": "HTML import failed", "url": source_url, "error": str(e)})
            return {"ok": False, "orgURL": source_url, "reason": import_error(e)}
        try:
            return import_summary(created)
        except Exception as e:
            error_msg = f"Imported the recipe but could not read the result: {e}"
            logger.error({"message": error_msg})
            logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
            raise ToolError(error_msg)

    @mcp.tool()
    def set_recipe_image(slug: str, image_url: str) -> Dict[str, Any]:
        """Set or replace a recipe's picture from an image link. Mealie downloads the
        image.

        Args:
            slug: The recipe's slug
            image_url: A direct link to the image (jpg, png, webp)

        Returns:
            Dict with ok, slug and url (the recipe in Mealie, when MEALIE_PUBLIC_URL is set)
        """
        try:
            logger.info({"message": "Setting recipe image", "slug": slug, "url": image_url})
            mealie.scrape_recipe_image_from_url(slug, image_url)
            out: Dict[str, Any] = {"ok": True, "slug": slug}
            url = web_link(f"r/{slug}")
            if url:
                out["url"] = url
            return out
        except Exception as e:
            error_msg = f"Error setting the image for recipe '{slug}': {str(e)}"
            logger.error({"message": error_msg})
            logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
            raise ToolError(error_msg)

    @mcp.tool()
    def rename_recipe(slug: str, name: str) -> Dict[str, Any]:
        """Rename a recipe. Mealie changes the slug to match the new name, so use the
        slug in the reply from now on. Use it to drop a "(1)" or "(2)" suffix once the
        empty copies are deleted (a name still taken gets a new suffix).

        Args:
            slug: The recipe's current slug
            name: The new name

        Returns:
            Dict[str, Any]: The recipe, as get_recipe_detailed returns it, under its new slug
        """
        if not name or not name.strip():
            raise ToolError("The new name cannot be empty")
        try:
            logger.info({"message": "Renaming recipe", "slug": slug, "name": name})
            saved = mealie.patch_recipe(slug, {"name": name.strip()})
            return detailed(saved.get("slug", slug), saved)
        except Exception as e:
            error_msg = f"Error renaming recipe '{slug}': {str(e)}"
            logger.error({"message": error_msg})
            logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
            raise ToolError(error_msg)

    @mcp.tool()
    def delete_empty_recipe(slug: str) -> Dict[str, Any]:
        """Delete an empty recipe, such as one left behind by a failed save. Refuses any
        recipe with a description, linked ingredients, more than one ingredient or more
        than one step, so it cannot delete a real recipe. Check with get_recipes (search
        by name) which copy is the empty one before calling this.

        Args:
            slug: The empty recipe's slug

        Returns:
            Dict with ok, and the deleted recipe's slug and name
        """
        try:
            logger.info({"message": "Deleting empty recipe", "slug": slug})
            return {"ok": True, **mealie.delete_empty_recipe(slug)}
        except Exception as e:
            error_msg = f"Recipe '{slug}' was not deleted: {str(e)}"
            logger.error({"message": error_msg})
            raise ToolError(error_msg)

    @mcp.tool()
    def duplicate_recipe(slug: str, name: Optional[str] = None) -> Dict[str, Any]:
        """Clone an existing recipe under a new name. Use this before making
        significant variations (e.g., a vegetarian version, a scaled-up batch).

        Args:
            slug: The unique text identifier of the recipe to clone
            name: Name for the new copy (defaults to original name with copy indicator)

        Returns:
            Dict[str, Any]: The new copy, as get_recipe_detailed returns it (use its
                slug from now on)
        """
        try:
            logger.info({"message": "Duplicating recipe", "slug": slug, "name": name})
            copy = mealie.duplicate_recipe(slug, name=name)
            return detailed(copy["slug"], copy)
        except Exception as e:
            error_msg = f"Error duplicating recipe '{slug}': {str(e)}"
            logger.error({"message": error_msg})
            logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
            raise ToolError(error_msg)

    @mcp.tool()
    def mark_recipe_last_made(slug: str) -> Dict[str, Any]:
        """Mark a recipe as having been made today (updates last made timestamp).

        Args:
            slug: The unique text identifier for the recipe.

        Returns:
            Dict[str, Any]: name, slug and lastMade — the household's last made date,
                read back after the update, as get_recipe_detailed reports it
        """
        try:
            logger.info({"message": "Marking recipe as last made", "slug": slug})
            recipe = mealie.update_recipe_last_made(slug)
            return add_last_made(slug, {"name": recipe.get("name"), "slug": recipe.get("slug", slug)})
        except Exception as e:
            error_msg = f"Error updating recipe last made '{slug}': {str(e)}"
            logger.error({"message": error_msg})
            logger.debug({"message": "Error traceback", "traceback": traceback.format_exc()})
            raise ToolError(error_msg)


