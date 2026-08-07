"""Typer CLI, mirroring the MCP tools one-to-one.

Same pattern as apthunt and doctolib: everything is reachable without an MCP client, so a
broken tool reproduces in one shell command instead of an MCP session. That is the whole
point of this layer -- it is the debugging surface, not a convenience.
"""
from __future__ import annotations

import json

import typer

from . import client, session
from .errors import GoogleError

app = typer.Typer(add_completion=False, help="Google Search through a dedicated logged-in profile.")


def _emit(payload) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


@app.command()
def login() -> None:
    """Open a window and sign in once. Closes itself when the sign-in lands."""
    typer.echo("Opening a window on the dedicated profile.")
    typer.echo("Sign in to the account you want searches personalized to.")
    typer.echo("The window closes on its own once you are signed in; closing it by hand also works.")
    typer.echo("")
    typer.echo("  This profile is separate from your Chrome, so Chrome's /u/0 default")
    typer.echo("  does not apply -- whatever you sign in as here is what gets used.")
    result = session.login()
    if result["signed_in"]:
        typer.echo(f"\nSigned in as {result['account']}.")
    else:
        typer.echo("\nNo session detected. Re-run and complete the sign-in, or check `cli status`.")
    _emit(result)


@app.command()
def status() -> None:
    """Is the profile signed in, and as whom."""
    _emit(session.status())


@app.command()
def search(
    query: str,
    pages: int = typer.Option(1, help="10 results per page, max 5."),
    site: str = typer.Option(None, help="site: operator"),
    filetype: str = typer.Option(None, help="filetype: operator, e.g. pdf"),
    exact: str = typer.Option(None, help='exact phrase, quoted'),
    exclude: list[str] = typer.Option(None, help="-term, repeatable"),
    before: str = typer.Option(None, help="before:YYYY-MM-DD"),
    after: str = typer.Option(None, help="after:YYYY-MM-DD"),
    lang: str = typer.Option(None, help="hl, e.g. en / de"),
    country: str = typer.Option(None, help="two-letter, e.g. de"),
    freshness: str = typer.Option(None, help="hour|day|week|month|year"),
    vertical: str = typer.Option("web", help="web|web_only|news|videos|short_videos|books|images"),
    personalized: bool = typer.Option(True, help="--no-personalized sends pws=0"),
    verbatim: bool = typer.Option(False, help="no synonyms or stemming (tbs li:1)"),
    strict_dates: bool = typer.Option(False, help="before/after as Tools>Custom range, not operators"),
    with_content: bool = typer.Option(False, help="also read the top results as markdown"),
    content_top_n: int = typer.Option(3, help="how many results to read"),
    content_chars: int = typer.Option(2000, help="markdown budget per page"),
) -> None:
    """One query, ads stripped, full operator surface."""
    try:
        _emit(
            client.search(
                query, pages=pages, site=site, filetype=filetype, exact=exact,
                exclude=exclude, before=before, after=after, lang=lang,
                country=country, freshness=freshness, vertical=vertical,
                personalized=personalized, verbatim=verbatim, strict_dates=strict_dates,
                with_content=with_content, content_top_n=content_top_n,
                content_chars=content_chars,
            )
        )
    except GoogleError as exc:
        _emit(exc.as_result())
        raise typer.Exit(1)


@app.command()
def fetch(
    urls: list[str],
    max_chars: int = typer.Option(2000, help="markdown budget per page"),
) -> None:
    """Read pages as markdown through the warmed browser. Boilerplate stripped."""
    try:
        _emit(client.fetch(urls, max_chars=max_chars))
    except GoogleError as exc:
        _emit(exc.as_result())
        raise typer.Exit(1)


@app.command("ai-mode")
def ai_mode(
    query: str,
    lang: str = typer.Option(None, help="hl, e.g. en / de"),
    country: str = typer.Option(None, help="two-letter, e.g. de"),
) -> None:
    """Google's AI Mode answer. Hit or miss and not authoritative -- read the citations."""
    try:
        _emit(client.ai_mode(query, lang=lang, country=country))
    except GoogleError as exc:
        _emit(exc.as_result())
        raise typer.Exit(1)


@app.command("multi-search")
def multi_search(
    queries: list[str],
    pages: int = typer.Option(1, help="10 results per page, max 5."),
    personalized: bool = typer.Option(True),
) -> None:
    """Several queries through one warmed browser. The compound tool."""
    _emit(client.multi_search(list(queries), pages=pages, personalized=personalized))


@app.command()
def shutdown() -> None:
    """Close the browser this process opened."""
    session.shutdown()
    _emit({"ok": True})


if __name__ == "__main__":
    app()
