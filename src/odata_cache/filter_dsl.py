"""DSL фильтра $filter: лексер + рекурсивный спуск → AST.

Поддерживается смешанный стиль (решение Q4-a «а»):
  - канон OData:  eq ne gt ge lt le `in` is null / is not null, and or not,
                  contains(f,'x') startswith(f,'x') endswith(f,'x')
  - sugar:        = < > <= >= <>  и ~ (подстрока без учёта регистра)
  - литералы:     'строка' ('' внутри — экранирование), числа, true/false/null,
                  guid'...', datetime'...', date'...'
  - скобки:       ( ... ) и { ... } — равнозначны (URL-безопасная форма)

Безопасность: выражение парсится в AST; компиляция в SQL выполняется только
по полям, предварительно проверенным по реестру (см. filter_compile.py).
Никакой подстановки строк пользователя в SQL здесь нет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_FILTER_LENGTH = 2000
MAX_DEPTH = 8


class FilterSyntaxError(ValueError):
    """Ошибка синтаксиса фильтра с позицией в исходной строке."""

    def __init__(self, message: str, position: int) -> None:
        super().__init__(f"{message} (position {position})")
        self.message = message
        self.position = position

    def to_dict(self) -> dict[str, Any]:
        return {"code": "filter_parse", "message": self.message,
                "position": self.position}


# ---------------------------------------------------------------------------
# Токены
# ---------------------------------------------------------------------------

OP_SYMBOLS = {
    "=": "eq", "<>": "ne", "!=": "ne", "~": "contains_sugar",
    ">=": "ge", "<=": "le", ">": "gt", "<": "lt",
}

KEYWORD_OPS = {"eq", "ne", "gt", "ge", "lt", "le"}
# словесные синонимы операторов (like — из эскиза пользователя, = contains)
KEYWORD_OP_ALIASES = {"like": "contains_sugar"}
FUNCTIONS = {"contains", "startswith", "endswith"}
# зарезервированные слова: операторы/логика/литералы — не могут быть именами полей
RESERVED_WORDS = KEYWORD_OPS | set(KEYWORD_OP_ALIASES) | {
    "and", "or", "not", "in", "is", "true", "false", "null",
}


def strip_filter_prefix(source: str) -> str:
    """Срезает ``$filter`` и обрамляющие ``{}`` — эскиз пользователя допускает
    передачу строки целиком: ``$filter{Description like 'халат'}``.
    """
    s = source.strip()
    if s.lower().startswith("$filter"):
        s = s[len("$filter"):]
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    return s

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<tstring>(?:guid|datetime|date)\s*'[^']*')   # типизированный литерал
  | (?P<string>'(?:[^']|'')*')                      # обычная строка
  | (?P<number>[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<op><>|!=|>=|<=|[=<>~])
  | (?P<lparen>[\(\[\{])
  | (?P<rparen>[\)\]\}])
  | (?P<comma>,)
  | (?P<word>[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9]*)
    """,
    re.VERBOSE | re.UNICODE,
)


@dataclass(frozen=True)
class Token:
    kind: str          # ident | string | number | op | lparen | rparen | comma
    value: str         # нормализованное значение (op: канонический оператор)
    pos: int           # позиция в исходной строке
    literal: str = ""  # для tstring: префикс типа (guid/datetime/date)


def tokenize(source: str, end_pos: int | None = None) -> list[Token]:
    """Токены фильтра. end_pos — длина исходной строки $filter (для точных
    позиций ошибок в обрубленных выражениях)."""
    if len(source) > MAX_FILTER_LENGTH:
        raise FilterSyntaxError(
            f"фильтр длиннее {MAX_FILTER_LENGTH} символов", len(source))
    tokens: list[Token] = []
    i = 0
    n = len(source)
    while i < n:
        m = _TOKEN_RE.match(source, i)
        if not m:
            raise FilterSyntaxError(f"неожиданный символ {source[i]!r}", i)
        i = m.end()
        kind = m.lastgroup
        assert kind is not None
        if kind == "ws":
            continue
        text = m.group()
        pos = m.start()
        if kind == "op":
            # символьный оператор не может быть именем поля — фиксируем op
            tokens.append(Token("op", OP_SYMBOLS[text], pos))
        elif kind == "tstring":
            prefix_len = text.index("'")
            prefix = text[:prefix_len].strip().lower()
            raw = text[prefix_len + 1:-1]
            tokens.append(Token("string", raw, pos, literal=prefix))
        elif kind == "string":
            tokens.append(Token("string", text[1:-1].replace("''", "'"), pos))
        elif kind == "number":
            tokens.append(Token("number", text, pos))
        elif kind == "word":
            low = text.lower()
            if low in RESERVED_WORDS:
                tokens.append(Token("kw", low, pos))
            else:
                tokens.append(Token("ident", text, pos))
        else:  # lparen | rparen | comma
            tokens.append(Token(kind, text, pos))
    # маркер конца строки — для корректных позиций «неожиданного конца»
    tokens.append(Token("eof", "", end_pos if end_pos is not None else n))
    return tokens


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

@dataclass
class Node:
    pass


@dataclass
class Compare(Node):
    """Поле <оп> значение. op: eq ne gt ge lt le contains startswith endswith."""
    field_name: str
    op: str
    value: Any  # str | int | float | bool | None
    pos: int = 0


@dataclass
class InList(Node):
    field_name: str
    items: list[Any]
    negated: bool = False
    pos: int = 0


@dataclass
class IsNull(Node):
    field_name: str
    negated: bool
    pos: int = 0


@dataclass
class Logic(Node):
    op: str            # 'and' | 'or'
    children: list[Node] = field(default_factory=list)


@dataclass
class Not(Node):
    child: Node


# ---------------------------------------------------------------------------
# Парсер (рекурсивный спуск)
# ---------------------------------------------------------------------------

class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.i = 0
        self.depth = 0

    # --- утилиты ---
    def peek(self, offset: int = 0) -> Token | None:
        """Токен или None на eof."""
        j = self.i + offset
        if j >= len(self.tokens):
            return None
        tok = self.tokens[j]
        return None if tok.kind == "eof" else tok

    def next(self) -> Token:
        tok = self.peek()
        if tok is None:
            last = self.tokens[-1]  # маркер eof с позицией конца строки
            raise FilterSyntaxError("неожиданный конец выражения", last.pos)
        self.i += 1
        return tok

    def expect_kw(self, word: str) -> Token:
        tok = self.next()
        if not (tok.kind == "kw" and tok.value == word):
            raise FilterSyntaxError(f"ожидалось '{word}'", tok.pos)
        return tok

    def expect_field(self) -> Token:
        tok = self.next()
        if tok.kind != "ident":
            raise FilterSyntaxError("ожидалось имя поля", tok.pos)
        return tok

    # --- скобки: () [] {} взаимозаменяемы, но парные ---
    _CLOSE_FOR = {r"(": r")", r"[": r"]", r"{": r"}"}

    def eat_open(self, what: str = "открывающая скобка") -> str:
        tok = self.next()
        if tok.kind != "lparen" or tok.value not in self._CLOSE_FOR:
            raise FilterSyntaxError(f"ожидалась {what}", tok.pos)
        return tok.value

    def eat_close(self, opener: str) -> None:
        tok = self.next()
        if tok.kind != "rparen" or tok.value != self._CLOSE_FOR[opener]:
            raise FilterSyntaxError(
                f"ожидалась '{self._CLOSE_FOR[opener]}'", tok.pos)

    # --- грамма ---
    def parse(self) -> Node:
        if self.peek() is None:
            raise FilterSyntaxError("пустой фильтр", 0)
        node = self.parse_or()
        if self.peek() is not None:
            raise FilterSyntaxError("лишние токены после выражения",
                                    self.next().pos)
        return node

    def parse_or(self) -> Node:
        left = self.parse_and()
        while (t := self.peek()) and t.kind == "kw" and t.value == "or":
            self.next()
            right = self.parse_and()
            if isinstance(left, Logic) and left.op == "or":
                left.children.append(right)
            else:
                left = Logic("or", [left, right])
        return left

    def parse_and(self) -> Node:
        left = self.parse_unary()
        while True:
            t = self.peek()
            if t is None:
                break
            join_and = False
            if t.kind == "kw" and t.value == "and":
                join_and = True
                self.next()
            elif t.kind == "comma":
                # запятая между предикатами == and (эскиз: in [...], Desc like '...')
                join_and = True
                self.next()
            if not join_and:
                break
            right = self.parse_unary()
            if isinstance(left, Logic) and left.op == "and":
                left.children.append(right)
            else:
                left = Logic("and", [left, right])
        return left

    def parse_unary(self) -> Node:
        t = self.peek()
        if t and t.kind == "kw" and t.value == "not":
            self.next()
            return Not(self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> Node:
        t = self.peek()
        if t is None:
            raise FilterSyntaxError("неожиданный конец выражения",
                                    self.tokens[-1].pos if self.tokens else 0)
        if t.kind in ("lparen",):
            self._enter(t.pos)
            opener = self.next().value
            node = self.parse_or()
            self.eat_close(opener)
            self.depth -= 1
            return node
        if t.kind == "ident" and t.value.lower() in FUNCTIONS:
            return self.parse_function()
        return self.parse_comparison()

    def parse_function(self) -> Node:
        name_tok = self.next()
        fn = name_tok.value.lower()
        opener = self.eat_open(f"открывающая скобка после {fn}(")
        field_tok = self.expect_field()
        comma = self.next()
        if comma.kind != "comma":
            raise FilterSyntaxError("ожидалась запятая", comma.pos)
        value = self.parse_literal_value()
        self.eat_close(opener)
        return Compare(field_tok.value, fn, value, pos=name_tok.pos)

    def parse_comparison(self) -> Node:
        field_tok = self.expect_field()
        fname = field_tok.value
        t = self.peek()
        if t is None:
            raise FilterSyntaxError(f"после '{fname}' ожидался оператор",
                                    field_tok.pos)

        # not in / in
        if t.kind == "kw" and t.value == "not" and \
                (nxt := self.peek(1)) and nxt.kind == "kw" and nxt.value == "in":
            self.next(); self.next()
            return self.parse_in_list(fname, negated=True, pos=t.pos)
        if t.kind == "kw" and t.value == "in":
            self.next()
            return self.parse_in_list(fname, negated=False, pos=t.pos)

        # is [not] null
        if t.kind == "kw" and t.value == "is":
            self.next()
            negated = False
            if (nxt := self.peek()) and nxt.kind == "kw" and nxt.value == "not":
                self.next()
                negated = True
            null_tok = self.next()
            if not (null_tok.kind == "kw" and null_tok.value == "null"):
                raise FilterSyntaxError("ожидалось 'null'", null_tok.pos)
            return IsNull(fname, negated=negated, pos=t.pos)

        # оператор сравнения: символьный (= ~ > ...) или словесный (eq ne gt ge lt le like)
        op = None
        if t.kind == "op":
            op = t.value
        elif t.kind == "kw" and t.value in KEYWORD_OPS:
            op = t.value
        elif t.kind == "kw" and t.value in KEYWORD_OP_ALIASES:
            op = KEYWORD_OP_ALIASES[t.value]
        if op is not None:
            if op == "contains_sugar":
                op = "contains"
            self.next()
            value = self.parse_literal_value()
            return Compare(fname, op, value, pos=t.pos)

        # скажем прямо про незнакомое
        raise FilterSyntaxError(
            f"ожидался оператор (=, ~, eq, in, is null...), получено '{t.value}'",
            t.pos)

    def parse_in_list(self, fname: str, *, negated: bool, pos: int) -> Node:
        opener = self.eat_open("список в () / [] / {}")
        items: list[Any] = []
        if (nxt := self.peek()) and nxt.kind == "rparen" \
                and nxt.value == self._CLOSE_FOR[opener]:
            self.next()
            return InList(fname, items, negated=negated, pos=pos)
        while True:
            items.append(self.parse_literal_value())
            sep = self.next()
            if sep.kind == "comma":
                continue
            if sep.kind == "rparen" and sep.value == self._CLOSE_FOR[opener]:
                break
            raise FilterSyntaxError("ожидалась ',' или закрывающая скобка", sep.pos)
        return InList(fname, items, negated=negated, pos=pos)

    def parse_literal_value(self) -> Any:
        t = self.next()
        if t.kind == "string":
            return t.value
        if t.kind == "number":
            return int(t.value) if re.fullmatch(r"[+-]?\d+", t.value) \
                else float(t.value)
        if t.kind == "kw" and t.value in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[t.value]
        raise FilterSyntaxError(
            f"ожидалось значение (строка/число/дата/true/false/null), "
            f"получено '{t.value}'", t.pos)

    def _enter(self, pos: int) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise FilterSyntaxError(
                f"слишком глубокая вложенность (>{MAX_DEPTH})", pos)


def parse_filter(source: str) -> Node:
    """Строка $filter → AST. Допускается префикс ``$filter`` и обрамляющие ``{}``
    (эскиз пользователя). Бросает FilterSyntaxError с позицией."""
    return Parser(tokenize(strip_filter_prefix(source))).parse()


# ---------------------------------------------------------------------------
# Интроспекция AST (для компилятора и валидации полей)
# ---------------------------------------------------------------------------

def iter_fields(node: Node) -> list[str]:
    """Имена всех полей, упомянутых в фильтре (с дублями, порядок сохранения)."""
    out: list[str] = []

    def walk(n: Node) -> None:
        if isinstance(n, (Compare, InList, IsNull)):
            out.append(n.field_name)
        elif isinstance(n, Logic):
            for c in n.children:
                walk(c)
        elif isinstance(n, Not):
            walk(n.child)

    walk(node)
    return out
