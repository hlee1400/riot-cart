"""Recipe schema: the store-specific targets the fixed cart flow needs."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class RecipeError(Exception):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoleLocator(_Strict):
    role: str
    name: str
    exact: bool = False


class CssLocator(_Strict):
    css: str


Locator = RoleLocator | CssLocator
# Alternatives tried in order: primary first, then fallbacks.
Target = Annotated[list[Locator], Field(min_length=1)]


class OptionGroup(_Strict):
    name: str
    aliases: list[str] = Field(default_factory=list)  # other words users say for this choice
    kind: Literal["buttons", "select", "swatches", "radio"]
    group: Target
    option: Target  # resolved within `group`
    value_label: Literal["text", "aria-label", "value", "title"] = "text"
    selected_css: str  # tested with element.matches() on an option element
    unavailable_css: str | None = None
    required: bool = True


class Search(_Strict):
    url_template: str | None = None
    input: Target | None = None
    submit: Target | None = None
    result_item: Target
    result_title: Target
    result_link: Target

    @model_validator(mode="after")
    def _one_mode(self) -> "Search":
        if (self.url_template is None) == (self.input is None):
            raise ValueError("search needs exactly one of url_template or input")
        if self.url_template is not None and "{query}" not in self.url_template:
            raise ValueError("search.url_template must contain {query}")
        return self


class Product(_Strict):
    title: Target
    price: Target
    add_to_cart: Target
    quantity_input: Target | None = None
    added_signal: Target | None = None
    option_groups: list[OptionGroup] = Field(default_factory=list)


class Cart(_Strict):
    url: str | None = None  # a cart page, or...
    open_button: Target | None = None  # ...a header button that opens a cart drawer
    line_item: Target
    line_title: Target
    line_quantity: Target
    line_price: Target | None = None
    line_options: Target | None = None
    quantity_control: Target | None = None  # an input to type the quantity into
    quantity_increase: Target | None = None  # a "+" control inside the line

    @model_validator(mode="after")
    def _one_way_in(self) -> "Cart":
        if (self.url is None) == (self.open_button is None):
            raise ValueError("cart needs exactly one of url or open_button")
        return self


class Blockers(_Strict):
    login_css: list[str] = Field(default_factory=list)
    challenge_css: list[str] = Field(default_factory=list)


class Recipe(_Strict):
    schema_version: Literal[1]
    recipe_version: str
    retailer: str
    base_url: str
    allowed_origins: list[str] = Field(min_length=1)
    allowed_path_prefixes: list[str] = Field(min_length=1)
    cookie_banner_accept: Target | None = None
    search: Search
    product: Product
    cart: Cart
    blockers: Blockers = Field(default_factory=Blockers)


def load_recipe(path: Path | str) -> Recipe:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return Recipe.model_validate(data)
    except (ValidationError, yaml.YAMLError) as e:
        raise RecipeError(f"invalid recipe {path}: {e}") from e
