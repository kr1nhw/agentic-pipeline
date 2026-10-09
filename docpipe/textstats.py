"""Text statistics that more than one part of the pipeline has to agree on.

These live outside any format module because two very different callers ask the
same question and must get the same answer:

* the PDF reader, deciding whether a text layer was decoded or guessed at;
* the evaluator, deciding whether a page's output is text or debris.

They drifted once. The reader used word shape and was calibrated against
documents whose answer was known; the evaluator used "share of alphanumeric
characters", which counts newlines and indentation — so a chart page, whose text
is one number per line, scored 0.27 and 110 pages of the corpus were flagged as
garbled while the one genuinely mis-decoded document in reach scored 0.90 on the
same measure. Two definitions of one idea, neither aware of the other.
"""

from __future__ import annotations

#: Fraction of tokens that must look like words before text is believed.
#:
#: Measured, not guessed. On the documents whose answer is known by eye:
#:
#:   topology paper   (1950s scan)  word shape 0.41   text GARBLED, unusable
#:   1982 survey                    word shape 0.92   text correct, rare substitutions
#:   Congressional Record           word shape 0.95   text correct
#:   2021 ACL paper                 word shape 0.92   text correct
#:   IRS form 1040                  word shape 0.86   text correct, sparse
#:
#: The threshold sits between the one document that must be refused and the rest,
#: and the margin is wide enough to be a rule rather than a fit.
WORD_SHAPE_FLOOR = 0.60

_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_EDGE = ".,;:!?()[]{}<>\"'`/*_+=|\\"

#: Below this many tokens there is not enough evidence to convict anything.
MIN_TOKENS = 20


#: A font table this unmapped is what a scan put through OCR looks like: the
#: characters were inferred from glyph names rather than read from a CMap.
UNICODE_MAP_FLOOR = 0.15


def layer_looks_misdecoded(unicode_ratio: float, word_shape: float) -> bool:
    """Is this text layer debris? Two pieces of evidence, and both are required.

    Font metadata says the characters *might* have been guessed at; the text says
    whether the guess landed. Either one alone is wrong in a way that has already
    cost real work:

    * **Fonts alone** flagged the Congressional Record (329 unembedded fonts, no
      unicode maps, text perfect), the Federal Register, and a 1982 survey whose
      text reads `M. Jarke and J. Koch` — 51 pages and a 300-page issue sent to
      vision for text that was never wrong.
    * **Word shape alone** flags mathematics. Measured on a fresh corpus: 166 pages
      of formula-dense arXiv papers scored 0.14–0.56, because `σij,ts`, `(eit`,
      `OP (1)` are not words. The measure cannot tell notation from debris — the
      genuinely mis-decoded document to hand scored 0.41, inside their range.

    Together they separate all four cases whose answer is known by eye:

    | document | unicode ratio | word shape | verdict |
    | --- | --- | --- | --- |
    | 1950s scan, layer guessed from glyph names | 0.00 | **0.41** | debris |
    | 1982 survey | 0.00 | 0.96 | text |
    | Congressional Record | 0.00 | 0.95 | text |
    | formula-dense arXiv papers | 0.77 | 0.14–0.56 | text |

    The reader has always applied it this way. The evaluator applied word shape on
    its own, which is why it reported 166 pages of correct mathematics as garbled.
    """
    return unicode_ratio < UNICODE_MAP_FLOOR and word_shape < WORD_SHAPE_FLOOR


def text_word_shape(text: str, *, letters_only: bool = False) -> float:
    """Fraction of tokens that are made of letters.

    Prose is built of letter-only tokens. A layer decoded from glyph names is
    built of fragments like ``~'(R)``, ``R tl]`` and ``U{Ry`` — letters padded
    with the symbols whose glyph names happened to be nearby. Deliberately
    language-neutral: it asks for the shape of a word, not for a dictionary, so a
    page in a language this was never tested on is not punished for it.

    A word may contain a hyphen, an apostrophe or an underscore: `place_of_birth`
    is a word, and a rule that calls it debris would flag every data-visualisation
    label and every identifier on a page as a decoding failure.
    A chart's numbers cannot be misspelled by a decoder, so

    ``letters_only`` restricts the vote to tokens containing a letter. A chart or
    a table of results is mostly numbers, and numbers cannot be misspelled by a
    decoder: letting them vote made a page of axis labels look like debris. Use
    it when judging a page that may legitimately be numeric.
    """
    tokens = [token for token in text.split() if any(ch.isalnum() for ch in token)]
    if letters_only:
        tokens = [token for token in tokens if any(ch in _LETTERS for ch in token)]
    if len(tokens) < MIN_TOKENS:
        return 1.0
    good = 0
    for token in tokens:
        core = token.strip(_EDGE)
        if len(core) >= 2 and all(ch in _LETTERS or ch in "-'_" for ch in core):
            good += 1
    return good / len(tokens)
