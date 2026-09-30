"""What the library asks of DIDA, the house's automation.

The contacts book is the household's, not the library's. DIDA reads it — one
consent screen, one token to expire, one schedule — and this asks. A birth date
is needed here for exactly one thing: two children who look alike at the same age
are told apart by nothing else, and the machine's own estimate stands in until
somebody has written the real one down. That somebody wrote it in a contacts
book, and this library has no business keeping a second copy of it.

Read only, and never the reverse: DIDA asks this module for nothing."""

from opus_core import dida


async def birth_date(config, name: str) -> dict | None:
    """When somebody was born, according to the household's book.

    Returns nothing for a name the book does not hold, and nothing for one it
    holds twice — DIDA refuses an ambiguous name rather than guessing, and it is
    right to: handing back one of two same-named people would put a birth year on
    the wrong face, and the whole reason a birth year is wanted here is to keep
    two faces apart."""
    try:
        return await dida.call(config, "GET", "/api/contacts/person", params={"name": name})
    except dida.Absent:
        return None


async def birthdays(config, within: int = 2) -> list:
    """Whose birthday falls in the next few days. Not used to decide anything
    here — the house says that out loud — but a library that knows who is in a
    photograph can offer the right ones on the right morning."""
    return await dida.call(config, "GET", "/api/contacts/birthdays", params={"within": within})
