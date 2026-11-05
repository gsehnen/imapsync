#!/usr/bin/env python3
"""Migração de contas de e-mail em lote usando o imapsync.

Uso interativo (pergunta quantidade de contas, servidores e credenciais):
    ./migrar_email.py

Uso com arquivo CSV (cabeçalho obrigatório, separador "," ou ";"):
    ./migrar_email.py --csv contas.csv --origem imap.antigo.com --destino imap.novo.com

    usuario_origem;senha_origem;usuario_destino;senha_destino
    joao@empresa.com;s3nha;joao@empresa.com;0utra
    maria@empresa.com;s3nha2;;

    Destino vazio = mesmo usuário/senha da origem.

Repetir só o que não terminou OK na última execução:
    ./migrar_email.py --repetir-falhas

Opções extras do imapsync vão depois de "--":
    ./migrar_email.py --csv contas.csv -- --automap --maxsize 50000000

Rodar de novo é seguro: o imapsync compara os cabeçalhos das mensagens e copia
apenas o que ainda não existe no destino.
"""

from __future__ import annotations

import argparse
import collections
import csv
import dataclasses
import datetime
import getpass
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

STATUS_OK = "OK"
STATUS_ERRORS = "COM_ERROS"
STATUS_FAILED = "FALHOU"
STATUS_INTERRUPTED = "INTERROMPIDA"
STATUS_NOT_RUN = "NAO_EXECUTADA"

STATUS_LABELS = {
    STATUS_OK: "OK",
    STATUS_ERRORS: "Com erros",
    STATUS_FAILED: "Falhou",
    STATUS_INTERRUPTED: "Interrompida",
    STATUS_NOT_RUN: "Não executada",
}

# Códigos de saída definidos no próprio imapsync.
EXIT_REASONS = {
    1: "Erro inesperado do imapsync",
    6: "imapsync encerrado por sinal",
    7: "imapsync encerrado por arquivo de abort",
    8: "Erro no arquivo de PID",
    10: "Falha de conexão",
    12: "Falha ao negociar TLS",
    16: "Falha de autenticação",
    21: "Subpasta informada não existe na origem",
    64: "Opção inválida passada ao imapsync",
    101: "Não foi possível conectar na origem",
    102: "Não foi possível conectar no destino",
    111: "Ocorreram erros durante a sincronização",
    112: "Limite de erros atingido (--errorsmax); sincronização abortada",
    113: "Destino sem espaço (quota excedida)",
    114: "Destino recusou mensagens (APPEND)",
    115: "Falha ao ler mensagens da origem (FETCH)",
    116: "Falha ao criar pasta no destino",
    117: "Falha ao abrir pasta (SELECT)",
    118: "Limite de transferência excedido",
    119: "Destino recusou mensagem por vírus",
    120: "Falha ao gravar flags no destino",
    161: "Falha de autenticação na origem (usuário/senha)",
    162: "Falha de autenticação no destino (usuário/senha)",
}

# Erros que podem sumir sozinhos. Autenticação, quota etc. falhariam de novo.
RETRYABLE_EXITS = {10, 101, 102, 111, 112, 115}
RETRY_DELAY_SECONDS = 15
PREPARE_MAX_WORKERS = 8
MAX_STORED_ERRORS = 20
PLAIN_PROGRESS_SECONDS = 30

LEFT_RE = re.compile(r"(\d+)/(\d+) msgs left")
FOLDER_RE = re.compile(r"^Folder\s+(\d+)/(\d+) \[(.*?)\]\s+->")
SIZING_RE = re.compile(r"^Host1 folder\s+(\d+)/(\d+) \[")
COPIED_RE = re.compile(r"^msg .*? \{(\d+)\}\s+copied to ")
RATE_RE = re.compile(r"([\d.]+ \w+/s) [\d.]+ \w+ copied")
ERROR_RE = re.compile(r"^Err \d+/\d+: (.*)")
DRY_COUNT_RE = re.compile(r"^Messages transferred\s*:.*could be (\d+) without dry mode")
SKIPPED_RE = re.compile(r"^Messages skipped\s*:\s*(\d+)")
MISSING_RE = re.compile(r"^Messages found in host1 not in host2\s*:\s*(\d+)")
SOURCE_SIZE_RE = re.compile(r"^Host1 Total size:\s+(\d+) bytes")
SOURCE_COUNT_RE = re.compile(r"^Host1 Nb messages:\s+(\d+) messages")
SERVER_RE = re.compile(r"^(\[[0-9a-fA-F:.]+\]|[^\s:\[\]/]+)(?::(\d{1,5}))?$")


@dataclasses.dataclass
class Server:
    host: str
    port: int | None = None

    def __str__(self) -> str:
        return f"{self.host}:{self.port}" if self.port else self.host


@dataclasses.dataclass
class Account:
    index: int
    user1: str
    password1: str = dataclasses.field(repr=False)
    user2: str
    password2: str = dataclasses.field(repr=False)

    status: str = STATUS_NOT_RUN
    exit_code: int | None = None
    reason: str = ""
    errors: list[str] = dataclasses.field(default_factory=list)
    copied: int = 0
    copied_bytes: int = 0
    skipped: int = 0
    missing: int = 0
    attempts: int = 0
    duration: float = 0.0
    log_path: str = ""
    # Preenchidos por --medir; None = não medida.
    size_bytes: int | None = None
    size_messages: int | None = None

    # Progresso ao vivo, lido pela thread do painel.
    active: bool = False
    stage: str = "aguardando"
    percent: float = 0.0
    folder: str = ""
    msgs_left: int = 0
    msgs_total: int = 0
    rate: str = ""
    sync_started: float | None = None

    @property
    def label(self) -> str:
        return self.user1 if self.user1 == self.user2 else f"{self.user1} → {self.user2}"

    def reset_progress(self) -> None:
        self.stage = "conectando"
        self.percent = 0.0
        self.folder = ""
        self.msgs_left = self.msgs_total = 0
        self.rate = ""
        self.sync_started = None
        self.errors = []
        self.missing = 0

    def eta_seconds(self) -> float | None:
        done = self.msgs_total - self.msgs_left
        if not self.sync_started or done <= 0 or self.msgs_left <= 0:
            return None
        return (time.monotonic() - self.sync_started) * self.msgs_left / done


class ReportedInputError(Exception):
    """Entrada inválida, com mensagem pronta para o usuário."""


def printable(text: str) -> str:
    """Remove caracteres de controle vindos dos servidores antes de exibir."""
    return "".join(ch if ch.isprintable() else "?" for ch in text).strip()


def format_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def parse_server(text: str) -> Server:
    match = SERVER_RE.match(text.strip())
    if not match:
        raise ReportedInputError(f"Servidor inválido: {text!r}. Use host ou host:porta.")
    port = int(match.group(2)) if match.group(2) else None
    if port is not None and not 1 <= port <= 65535:
        raise ReportedInputError(f"Porta inválida: {port}.")
    return Server(match.group(1).strip("[]"), port)


# ---------------------------------------------------------------------------
# Entrada de dados
# ---------------------------------------------------------------------------


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("  Campo obrigatório.")


def ask_password(prompt: str, default: str | None = None) -> str:
    suffix = " [Enter = mesma da origem]" if default else ""
    while True:
        value = getpass.getpass(f"{prompt}{suffix}: ")
        if value:
            return value
        if default:
            return default
        print("  Campo obrigatório.")


def ask_count() -> int:
    while True:
        value = ask("Quantas contas serão migradas?")
        if value.isdigit() and int(value) >= 1:
            return int(value)
        print("  Informe um número inteiro maior que zero.")


def ask_server(prompt: str) -> Server:
    while True:
        try:
            return parse_server(ask(prompt))
        except ReportedInputError as error:
            print(f"  {error}")


def ask_yes(prompt: str, default: bool = True) -> bool:
    """Sem terminal para responder, assume o padrão."""
    if not sys.stdin.isatty():
        return default
    hint = "S/n" if default else "s/N"
    value = input(f"{prompt} [{hint}]: ").strip().lower()
    return default if not value else value in ("s", "sim", "y", "yes")


def read_accounts_interactive(count: int) -> list[Account]:
    accounts = []
    for index in range(1, count + 1):
        print(f"\nConta {index}/{count}")
        user1 = ask("  Usuário na origem")
        password1 = ask_password("  Senha na origem")
        user2 = ask("  Usuário no destino", default=user1)
        password2 = ask_password("  Senha no destino", default=password1)
        accounts.append(Account(index, user1, password1, user2, password2))
    return accounts


def read_accounts_csv(path: Path) -> list[Account]:
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            print(f"Aviso: {path} contém senhas e pode ser lido por outros usuários "
                  f"(sugestão: chmod 600 {path}).", file=sys.stderr)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            header = handle.readline()
            handle.seek(0)
            # O delimitador vem só do cabeçalho: senhas podem conter "," ou ";".
            reader = csv.DictReader(handle, delimiter=";" if ";" in header else ",")
            reader.fieldnames = [name.strip().lower() for name in reader.fieldnames or []]
            for column in ("usuario_origem", "senha_origem"):
                if column not in reader.fieldnames:
                    raise ReportedInputError(f"{path}: coluna obrigatória ausente: {column}.")
            accounts = []
            for row in reader:
                user1 = (row.get("usuario_origem") or "").strip()
                password1 = row.get("senha_origem") or ""
                if not user1 and not password1:
                    continue
                if not user1 or not password1:
                    raise ReportedInputError(
                        f"{path}, linha {reader.line_num}: usuário ou senha de origem vazio.")
                accounts.append(Account(
                    len(accounts) + 1,
                    user1,
                    password1,
                    (row.get("usuario_destino") or "").strip() or user1,
                    row.get("senha_destino") or password1,
                ))
    except OSError as error:
        raise ReportedInputError(f"Não foi possível ler {path}: {error.strerror}.") from error
    if not accounts:
        raise ReportedInputError(f"{path}: nenhuma conta encontrada.")
    return accounts


def check_duplicates(accounts: list[Account]) -> None:
    seen = collections.Counter((a.user1, a.user2) for a in accounts)
    repeated = [f"{u1} → {u2}" for (u1, u2), total in seen.items() if total > 1]
    if repeated:
        raise ReportedInputError("Contas repetidas: " + ", ".join(repeated))


def load_report(path: Path) -> dict:
    if path.is_dir():
        path = path / "relatorio.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ReportedInputError(
            f"Não foi possível ler o relatório {path}: {error.strerror}.") from error
    except ValueError as error:
        raise ReportedInputError(f"Relatório inválido em {path}.") from error


def accounts_to_retry(report: dict, csv_path: Path | None) -> list[Account]:
    pending = [c for c in report["contas"] if c["status"] != STATUS_OK]
    if not pending:
        return []
    known = {}
    if csv_path:
        known = {(a.user1, a.user2): a for a in read_accounts_csv(csv_path)}
    accounts = []
    print(f"{len(pending)} conta(s) a repetir:")
    for index, entry in enumerate(pending, 1):
        user1, user2 = entry["usuario_origem"], entry["usuario_destino"]
        print(f"  {index}. {user1} ({STATUS_LABELS.get(entry['status'], entry['status'])})")
        if csv_path:
            if (user1, user2) not in known:
                raise ReportedInputError(f"{csv_path}: conta {user1} → {user2} não encontrada.")
            source = known[(user1, user2)]
            accounts.append(Account(index, user1, source.password1, user2, source.password2))
        else:
            # O relatório nunca guarda senhas, então elas são pedidas de novo.
            password1 = ask_password(f"     Senha de {user1} na origem")
            password2 = ask_password(f"     Senha de {user2} no destino", default=password1)
            accounts.append(Account(index, user1, password1, user2, password2))
    return accounts


# ---------------------------------------------------------------------------
# Painel de progresso
# ---------------------------------------------------------------------------


class Dashboard:
    """Barras ao vivo no terminal; sem TTY, imprime linhas periódicas."""

    def __init__(self, accounts: list[Account], title: str):
        self.accounts = accounts
        self.title = title
        self.is_tty = sys.stdout.isatty()
        self.use_color = self.is_tty and "NO_COLOR" not in os.environ
        self.started = time.monotonic()
        self.weights, self.total_bytes = self._compute_weights(accounts)
        self._lock = threading.Lock()
        self._drawn_lines = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    @staticmethod
    def _compute_weights(accounts: list[Account]) -> tuple[list[float], int | None]:
        """Peso de cada conta na barra TOTAL: o tamanho, se medido; senão, todas iguais."""
        sizes = [a.size_bytes for a in accounts if a.size_bytes]
        if not sizes:
            return [1.0] * len(accounts), None
        # Conta cuja medição falhou entra com o tamanho médio das demais.
        average = sum(sizes) / len(sizes)
        weights = [average if a.size_bytes is None else float(a.size_bytes) for a in accounts]
        return weights, round(sum(weights))

    def _overall_fraction(self) -> float:
        done = 0.0
        for account, weight in zip(self.accounts, self.weights):
            if account.active:
                done += weight * account.percent / 100
            elif account.status not in (STATUS_NOT_RUN, STATUS_INTERRUPTED):
                done += weight
        return done / sum(self.weights)

    def start(self) -> None:
        self.started = time.monotonic()
        print(self.title)
        if self.is_tty:
            sys.stdout.write("\x1b[?25l")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()
        with self._lock:
            self._erase()
            for line in self._build_lines()[:1]:
                print(line)
            if self.is_tty:
                sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()

    def event(self, text: str) -> None:
        """Imprime uma linha permanente acima do painel."""
        with self._lock:
            self._erase()
            print(text)
            self._draw()

    def color(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.use_color else text

    def _loop(self) -> None:
        interval = 0.25 if self.is_tty else PLAIN_PROGRESS_SECONDS
        while not self._stop.wait(interval):
            with self._lock:
                self._erase()
                self._draw()

    def _erase(self) -> None:
        if self.is_tty and self._drawn_lines:
            sys.stdout.write(f"\x1b[{self._drawn_lines}A\r\x1b[J")
        self._drawn_lines = 0

    def _draw(self) -> None:
        lines = self._build_lines()
        if self.is_tty:
            # Nunca maior que a tela, senão o apagar do próximo quadro erra a linha.
            lines = lines[:max(1, shutil.get_terminal_size().lines - 2)]
            self._drawn_lines = len(lines)
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()

    def _bar(self, percent: float, width: int, code: str) -> str:
        filled = round(width * min(percent, 100.0) / 100)
        return self.color(code, "█" * filled) + "░" * (width - filled)

    def _build_lines(self) -> list[str]:
        columns = shutil.get_terminal_size().columns if self.is_tty else 120
        bar_width = 24 if columns >= 120 else 12
        counts = collections.Counter(a.status for a in self.accounts if not a.active)
        total = len(self.accounts)
        pending = (STATUS_NOT_RUN, STATUS_INTERRUPTED)
        finished = sum(1 for a in self.accounts if a.status not in pending and not a.active)
        running = [a for a in self.accounts if a.active]
        fraction = self._overall_fraction()
        elapsed = time.monotonic() - self.started

        # Contadores sempre aparecem; o resto só entra se couber sem quebrar a linha.
        room = max(0, columns - bar_width - 12)
        suffix = (f"{fraction * 100:5.1f}%  {finished}/{total} contas  "
                  f"ok {counts[STATUS_OK]}  erros {counts[STATUS_ERRORS]}  "
                  f"falhas {counts[STATUS_FAILED]}")
        optional = [format_duration(elapsed)]
        if 0.01 < fraction < 1:
            optional.append(f"ETA {format_duration(elapsed * (1 - fraction) / fraction)}")
        if self.total_bytes is not None:
            optional.append(f"{format_bytes(fraction * self.total_bytes)}"
                            f"/{format_bytes(self.total_bytes)}")
        for part in optional:
            if len(suffix) + 2 + len(part) <= room:
                suffix += f"  {part}"
        lines = [f" TOTAL   {self._bar(fraction * 100, bar_width, '1;36')} {suffix[:room]}"]

        name_width = min(30, max(columns - bar_width - 42, 10))
        for account in running:
            name = account.user1
            if len(name) > name_width:
                name = name[:name_width - 1] + "…"
            detail = f"{account.percent:5.1f}%  {self._account_detail(account)}"
            room = max(0, columns - name_width - bar_width - 6)
            lines.append(f"  {name:<{name_width}} {self._bar(account.percent, bar_width, '32')}"
                         f" {detail[:room]}")
        return lines

    @staticmethod
    def _account_detail(account: Account) -> str:
        parts = []
        if account.msgs_total:
            parts.append(f"{account.msgs_total - account.msgs_left}/{account.msgs_total} msgs")
        parts.append(account.folder or account.stage)
        if account.rate:
            parts.append(account.rate)
        eta = account.eta_seconds()
        if eta is not None:
            parts.append(f"ETA {format_duration(eta)}")
        if account.attempts > 1:
            parts.append(f"tentativa {account.attempts}")
        return "  ".join(parts)


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------


class Migration:
    def __init__(self, source: Server, target: Server, accounts: list[Account],
                 run_dir: Path, options: argparse.Namespace, extra_args: list[str]):
        self.source = source
        self.target = target
        self.accounts = accounts
        self.run_dir = run_dir
        self.logs_dir = run_dir / "logs"
        self.options = options
        self.extra_args = extra_args
        self.started_at = datetime.datetime.now().astimezone()
        self.ui: Dashboard | None = None
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen] = set()

    # ---- imapsync -----------------------------------------------------------

    def _command(self, account: Account, log_name: str, extra: list[str]) -> list[str]:
        command = ["imapsync", "--host1", self.source.host, "--user1", account.user1,
                   "--host2", self.target.host, "--user2", account.user2]
        if self.source.port:
            command += ["--port1", str(self.source.port)]
        if self.target.port:
            command += ["--port2", str(self.target.port)]
        command += ["--noreleasecheck", "--logdir", str(self.logs_dir), "--logfile", log_name,
                    "--pidfile", str(self.logs_dir / f"{log_name}.pid")]
        return command + extra + self.extra_args

    def _run(self, account: Account, log_name: str, extra: list[str], on_line) -> int | None:
        """Roda o imapsync e devolve o código de saída; None se a migração foi cancelada."""
        # Senhas vão por variável de ambiente: na linha de comando apareceriam no `ps`.
        environment = dict(os.environ,
                           IMAPSYNC_PASSWORD1=account.password1,
                           IMAPSYNC_PASSWORD2=account.password2)
        with self._lock:
            if self.stopping.is_set():
                return None
            # Sessão própria: o Ctrl-C do terminal não chega direto no imapsync,
            # que trataria um único Ctrl-C como pedido de reconexão.
            process = subprocess.Popen(
                self._command(account, log_name, extra),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", env=environment, start_new_session=True,
            )
            self._processes.add(process)
        tail = collections.deque(maxlen=3)
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            if line.strip() and not line.startswith(("Log file is", "Removing pidfile")):
                tail.append(printable(line))
            on_line(line)
        code = process.wait()
        with self._lock:
            self._processes.discard(process)
        if self.stopping.is_set() and code != 0:
            return None
        if code != 0 and not account.errors:
            account.errors = list(tail)
        return code

    def abort(self) -> None:
        """Encerra os imapsync em andamento; rodar de novo retoma de onde parou."""
        # Um segundo Ctrl-C não pode interromper a espera pelas threads.
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signal_number, signal.SIG_IGN)
        with self._lock:
            self.stopping.set()
            processes = list(self._processes)
        self._signal_all(processes, signal.SIGTERM)
        timer = threading.Timer(5, self._signal_all, (processes, signal.SIGKILL))
        timer.daemon = True
        timer.start()

    @staticmethod
    def _signal_all(processes: list[subprocess.Popen], signal_number: int) -> None:
        for process in processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal_number)
                except ProcessLookupError:
                    pass

    def _collect_error(self, account: Account, line: str) -> bool:
        match = ERROR_RE.match(line)
        if not match:
            return False
        if len(account.errors) < MAX_STORED_ERRORS:
            account.errors.append(printable(match.group(1)))
        return True

    # ---- validação de login -------------------------------------------------

    def _for_each_counting(self, function, accounts: list[Account]) -> None:
        """Roda a etapa preparatória em paralelo, mostrando o contador no terminal."""
        total = len(accounts)
        done = 0

        def counted(account: Account) -> None:
            nonlocal done
            function(account)
            with self._lock:
                done += 1
                if sys.stdout.isatty():
                    sys.stdout.write(f"\r  {done}/{total}")
                    sys.stdout.flush()

        self._for_each(min(PREPARE_MAX_WORKERS, total), counted, accounts)
        if sys.stdout.isatty():
            sys.stdout.write("\r\x1b[K")

    def preflight(self) -> list[Account]:
        """Testa o login de todas as contas e devolve as que falharam."""
        print(f"Validando login de {len(self.accounts)} conta(s)...")

        def check(account: Account) -> None:
            log_name = f"{account.index:03d}_{safe_name(account.user1)}.validacao.txt"
            code = self._run(account, log_name, ["--justlogin"],
                             lambda line: self._collect_error(account, line))
            if code not in (0, None):
                account.status = STATUS_FAILED
                account.exit_code = code
                account.reason = reason_for(code)
                account.attempts = 1
                account.log_path = str(self.logs_dir / log_name)

        self._for_each_counting(check, self.accounts)
        return [a for a in self.accounts if a.status == STATUS_FAILED]

    def measure(self, accounts: list[Account]) -> None:
        """Mede o tamanho das contas na origem, para a barra TOTAL andar por volume."""
        print(f"Medindo o tamanho de {len(accounts)} conta(s)...")
        # Contas fora da rodada (login recusado) não pesam na barra.
        for account in self.accounts:
            if account not in accounts:
                account.size_bytes = 0

        def check(account: Account) -> None:
            def on_line(line: str) -> None:
                match = SOURCE_SIZE_RE.match(line)
                if match:
                    account.size_bytes = int(match.group(1))
                match = SOURCE_COUNT_RE.match(line)
                if match:
                    account.size_messages = int(match.group(1))

            log_name = f"{account.index:03d}_{safe_name(account.user1)}.medicao.txt"
            code = self._run(account, log_name, ["--justfoldersizes"], on_line)
            if code != 0:
                account.size_bytes = account.size_messages = None
            # Um erro aqui não é o resultado da migração, que ainda vai rodar.
            account.errors = []

        self._for_each_counting(check, accounts)
        measured = [a for a in accounts if a.size_bytes is not None]
        if measured:
            biggest = max(measured, key=lambda a: a.size_bytes)
            print(f"Volume: {format_bytes(sum(a.size_bytes for a in measured))} em "
                  f"{sum(a.size_messages or 0 for a in measured)} mensagens  |  "
                  f"maior conta: {biggest.user1} ({format_bytes(biggest.size_bytes)})")
        if len(measured) < len(accounts):
            print(f"Aviso: {len(accounts) - len(measured)} conta(s) não puderam ser medidas "
                  "e entram na barra TOTAL com o tamanho médio.")

    def run(self, accounts: list[Account]) -> None:
        self.ui = Dashboard(self.accounts, f"\nMigrando {self.source} → {self.target}"
                            + (" (SIMULAÇÃO, nada será copiado)" if self.options.simular else ""))
        self.ui.start()
        try:
            self._for_each(self.options.paralelo, self._migrate_account, accounts)
        finally:
            self.ui.stop()

    def _for_each(self, workers: int, function, accounts: list[Account]) -> None:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(function, a) for a in accounts]
            try:
                for future in futures:
                    future.result()
            except KeyboardInterrupt:
                # Precisa ser aqui dentro: sair do `with` espera as threads,
                # e elas só terminam depois que os imapsync forem encerrados.
                self.abort()
                raise

    def _migrate_account(self, account: Account) -> None:
        if self.stopping.is_set():
            return
        began = time.monotonic()
        account.active = True
        code = None
        for attempt in range(1, self.options.tentativas + 1):
            account.attempts = attempt
            account.reset_progress()
            suffix = f"_tentativa{attempt}" if attempt > 1 else ""
            log_name = f"{account.index:03d}_{safe_name(account.user1)}{suffix}.txt"
            account.log_path = str(self.logs_dir / log_name)
            extra = ["--dry"] if self.options.simular else []
            code = self._run(account, log_name, extra,
                             lambda line, acc=account: self._on_line(acc, line))
            if code is None or code == 0 or code not in RETRYABLE_EXITS:
                break
            if attempt == self.options.tentativas:
                break
            self.ui.event(self.ui.color("33", f" ↻ {account.user1}: {reason_for(code)}. "
                                              f"Nova tentativa em {RETRY_DELAY_SECONDS}s."))
            account.stage = "aguardando nova tentativa"
            if self.stopping.wait(RETRY_DELAY_SECONDS):
                code = None
                break
        account.duration = time.monotonic() - began
        self._finish_account(account, code)

    def _on_line(self, account: Account, line: str) -> None:
        match = COPIED_RE.match(line)
        if match:
            account.copied += 1
            account.copied_bytes += int(match.group(1))
            rate = RATE_RE.search(line)
            if rate:
                account.rate = rate.group(1)
        match = LEFT_RE.search(line)
        if match:
            account.msgs_left, account.msgs_total = int(match.group(1)), int(match.group(2))
            if account.sync_started is None:
                account.sync_started = time.monotonic()
            if account.msgs_total:
                done = account.msgs_total - account.msgs_left
                account.percent = max(account.percent, 100.0 * done / account.msgs_total)
            return
        match = FOLDER_RE.match(line)
        if match:
            current, total = int(match.group(1)), int(match.group(2))
            account.folder = f"{printable(match.group(3))} ({current}/{total} pastas)"
            # Sem contagem de mensagens (--nofoldersizes) a barra anda por pasta.
            if not account.msgs_total:
                account.percent = max(account.percent, 100.0 * (current - 1) / total)
            return
        match = SIZING_RE.match(line)
        if match:
            account.stage = f"analisando pastas {match.group(1)}/{match.group(2)}"
            return
        if line.startswith("++++ End looping"):
            account.folder = ""
            account.stage = "conferindo resultado"
            return
        if self._collect_error(account, line):
            return
        match = DRY_COUNT_RE.match(line)
        if match:
            account.copied = int(match.group(1))
            return
        match = SKIPPED_RE.match(line)
        if match:
            account.skipped = int(match.group(1))
            return
        match = MISSING_RE.match(line)
        if match:
            account.missing = int(match.group(1))

    def _finish_account(self, account: Account, code: int | None) -> None:
        account.exit_code = code
        if code is None:
            account.status = STATUS_INTERRUPTED
            account.reason = "Interrompida pelo usuário"
        elif code == 0 and account.missing and not self.options.simular:
            account.status = STATUS_ERRORS
            account.reason = f"{account.missing} mensagem(ns) da origem não estão no destino"
        elif code == 0:
            account.status = STATUS_OK
            account.percent = 100.0
        elif 111 <= code <= 120:
            account.status = STATUS_ERRORS
            account.reason = reason_for(code)
        else:
            account.status = STATUS_FAILED
            account.reason = reason_for(code)
        account.active = False
        account.stage = ""
        with self._lock:
            self.write_report()
        self.ui.event(self._outcome_line(account))

    def _outcome_line(self, account: Account) -> str:
        if self.options.simular:
            summary = (f"{account.copied} seria(m) copiada(s), {account.skipped} já existia(m), "
                       f"em {format_duration(account.duration)}")
        else:
            summary = (f"{account.copied} copiada(s), {account.skipped} já existia(m), "
                       f"{format_bytes(account.copied_bytes)} em "
                       f"{format_duration(account.duration)}")
        if account.status == STATUS_OK:
            return self.ui.color("32", f" ✔ {account.label}: {summary}")
        code = "33" if account.status in (STATUS_ERRORS, STATUS_INTERRUPTED) else "31"
        icon = "⚠" if code == "33" else "✖"
        detail = f" | {account.errors[0]}" if account.errors else ""
        return self.ui.color(code, f" {icon} {account.label}: {account.reason}{detail}")

    # ---- relatórios ---------------------------------------------------------

    def write_report(self) -> None:
        entries = [{
            "usuario_origem": a.user1,
            "usuario_destino": a.user2,
            "status": a.status,
            "motivo": a.reason,
            "codigo_saida": a.exit_code,
            "copiadas": a.copied,
            "ja_existiam": a.skipped,
            "faltando_no_destino": a.missing,
            "bytes_copiados": a.copied_bytes,
            "tamanho_origem_bytes": a.size_bytes,
            "mensagens_origem": a.size_messages,
            "tentativas": a.attempts,
            "duracao_s": round(a.duration, 1),
            "log": a.log_path,
            "erros": a.errors,
        } for a in self.accounts]
        report = {
            "origem": str(self.source),
            "destino": str(self.target),
            "inicio": self.started_at.isoformat(timespec="seconds"),
            "atualizado_em": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "simulacao": self.options.simular,
            "opcoes_imapsync": self.extra_args,
            "contas": entries,
        }
        temporary = self.run_dir / "relatorio.json.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.run_dir / "relatorio.json")

        columns = [key for key in entries[0] if key != "erros"] + ["primeiro_erro"]
        with (self.run_dir / "relatorio.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for entry in entries:
                writer.writerow({**entry, "primeiro_erro": (entry["erros"] or [""])[0]})


def reason_for(code: int) -> str:
    if code < 0:
        return f"imapsync encerrado pelo sinal {-code}"
    return EXIT_REASONS.get(code, f"imapsync terminou com o código {code}")


def safe_name(text: str) -> str:
    return re.sub(r"[^\w.@+-]", "_", text)[:80]


def print_summary(migration: Migration) -> None:
    accounts = migration.accounts
    counts = collections.Counter(a.status for a in accounts)
    print("\n" + "=" * 60)
    print(f"Contas: {len(accounts)}  |  " + "  |  ".join(
        f"{STATUS_LABELS[status]}: {counts[status]}" for status in STATUS_LABELS if counts[status]))
    copied = sum(a.copied for a in accounts)
    skipped = sum(a.skipped for a in accounts)
    if migration.options.simular:
        print(f"Mensagens que seriam copiadas: {copied}  |  já existiam: {skipped}")
    else:
        print(f"Mensagens copiadas: {copied} "
              f"({format_bytes(sum(a.copied_bytes for a in accounts))})  |  já existiam: {skipped}")

    problems = [a for a in accounts if a.status != STATUS_OK]
    if problems:
        print("\nO que não terminou OK:")
        for account in problems:
            print(f"  [{STATUS_LABELS[account.status]}] {account.label}")
            if account.reason:
                print(f"      Motivo: {account.reason}")
            for error in account.errors[:3]:
                print(f"      {error[:200]}")
            if len(account.errors) > 3:
                print(f"      ... e mais {len(account.errors) - 3} erro(s)")
            if account.log_path:
                print(f"      Log: {account.log_path}")
    print(f"\nRelatório: {migration.run_dir / 'relatorio.csv'}")
    if problems and not migration.options.simular:
        print(f"Para repetir só essas contas: {Path(sys.argv[0]).name} --repetir-falhas")


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------


def parse_arguments(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    extra_args: list[str] = []
    if "--" in argv:
        split_at = argv.index("--")
        argv, extra_args = argv[:split_at], argv[split_at + 1:]

    def positive(text: str) -> int:
        value = int(text)
        if value < 1:
            raise argparse.ArgumentTypeError("precisa ser maior que zero")
        return value

    default_dir = Path(__file__).resolve().parent / "execucoes"
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--origem", help="servidor de origem (host ou host:porta)")
    parser.add_argument("--destino", help="servidor de destino (host ou host:porta)")
    parser.add_argument("--csv", type=Path, help="arquivo com as contas, em vez de digitar")
    parser.add_argument("--repetir-falhas", nargs="?", type=Path, const=default_dir / "ultima",
                        metavar="RELATORIO",
                        help="repete só as contas que não terminaram OK (padrão: última execução)")
    parser.add_argument("--paralelo", type=positive, default=1, metavar="N",
                        help="contas migradas ao mesmo tempo (padrão: 1)")
    parser.add_argument("--tentativas", type=positive, default=2, metavar="N",
                        help="tentativas por conta em erros temporários (padrão: 2)")
    parser.add_argument("--simular", action="store_true",
                        help="roda o imapsync com --dry: mostra o que faria, sem copiar nada")
    parser.add_argument("--sem-validacao", action="store_true",
                        help="não testa os logins antes de começar")
    parser.add_argument("--medir", action="store_true",
                        help="mede as contas antes de começar, para a barra TOTAL andar por "
                             "volume em vez de por conta")
    parser.add_argument("--dir-execucoes", type=Path, default=default_dir, metavar="DIR",
                        help="onde gravar logs e relatórios (padrão: ./execucoes)")
    options = parser.parse_args(argv)

    for argument in extra_args:
        if argument.startswith("--password"):
            parser.error("não passe senhas depois de \"--\": elas ficariam visíveis no `ps` "
                         "e gravadas no relatório.")
    return options, extra_args


def collect_inputs(options: argparse.Namespace, extra_args: list[str]):
    """Devolve (origem, destino, contas, opções extras)."""
    report = None
    if options.repetir_falhas:
        report = load_report(options.repetir_falhas)
        extra_args = extra_args or report.get("opcoes_imapsync", [])

    count = None
    if not report and not options.csv:
        count = ask_count()

    source_text = options.origem or (report and report["origem"])
    target_text = options.destino or (report and report["destino"])
    source = parse_server(source_text) if source_text else ask_server("Servidor de origem")
    target = parse_server(target_text) if target_text else ask_server("Servidor de destino")

    if report:
        accounts = accounts_to_retry(report, options.csv)
    elif options.csv:
        accounts = read_accounts_csv(options.csv)
    else:
        accounts = read_accounts_interactive(count)
    check_duplicates(accounts)
    return source, target, accounts, extra_args


def create_run_dir(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    name = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = base / name
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = base / f"{name}_{suffix}"
    # Logs e relatórios listam contas e erros de servidor: só o dono lê.
    run_dir.mkdir(mode=0o700)
    (run_dir / "logs").mkdir(mode=0o700)
    return run_dir


def point_latest_link(base: Path, run_dir: Path) -> None:
    temporary = base / "ultima.tmp"
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(run_dir.name)
    os.replace(temporary, base / "ultima")


def raise_keyboard_interrupt(_signal_number, _frame) -> None:
    raise KeyboardInterrupt


def main(argv: list[str]) -> int:
    options, extra_args = parse_arguments(argv)
    if not shutil.which("imapsync"):
        print("Erro: imapsync não encontrado no PATH.", file=sys.stderr)
        return 2

    try:
        source, target, accounts, extra_args = collect_inputs(options, extra_args)
    except ReportedInputError as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\nCancelado.", file=sys.stderr)
        return 130
    if not accounts:
        print("Nenhuma conta para repetir: todas terminaram OK na última execução.")
        return 0

    run_dir = create_run_dir(options.dir_execucoes)
    migration = Migration(source, target, accounts, run_dir, options, extra_args)
    migration.write_report()
    # Simulação não vira "última": --repetir-falhas continua apontando para a execução real.
    if not options.simular:
        point_latest_link(options.dir_execucoes, run_dir)

    # Queda da sessão SSH ou kill encerram igual ao Ctrl-C: relatório fica correto.
    signal.signal(signal.SIGTERM, raise_keyboard_interrupt)
    signal.signal(signal.SIGHUP, raise_keyboard_interrupt)

    interrupted = False
    try:
        to_run = accounts
        if not options.sem_validacao:
            failed = migration.preflight()
            if failed:
                print(f"\n{len(failed)} conta(s) não passaram na validação de login:")
                for account in failed:
                    print(f"  ✖ {account.label}: {account.reason}")
                to_run = [a for a in accounts if a.status != STATUS_FAILED]
                migration.write_report()
                if not to_run or not ask_yes(f"Continuar com as {len(to_run)} conta(s) válidas?"):
                    to_run = []
            else:
                print(f"Todos os {len(accounts)} logins OK.")
        if to_run:
            if options.medir:
                migration.measure(to_run)
                migration.write_report()
            migration.run(to_run)
    except (KeyboardInterrupt, EOFError):
        interrupted = True
        migration.abort()
        print("\nInterrompido. Rodar de novo retoma de onde parou.")

    migration.write_report()
    print_summary(migration)
    if interrupted:
        return 130
    return 0 if all(a.status == STATUS_OK for a in accounts) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
