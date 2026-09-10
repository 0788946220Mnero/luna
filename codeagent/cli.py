"""واجهة سطر الأوامر للوكيل البرمجي."""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from .agent import Agent, AgentStep
from .approvals import ApprovalManager, ApprovalRequest
from .backup import BackupManager, git_available, run_git
from .config import CONFIG_FILENAME, Config, load_config, write_default_config
from .executor import Executor
from .llm import LLMError, build_client
from .prompts import REVIEW_PROMPT
from .review import available_linters, run_review, summarize_issues
from .safety import SafetyError, relative_path, resolve_in_root
from .store import Store
from .tools import all_tools
from .tools.tests import detect_test_command
from .walker import analyze_project

console = Console()

RISK_COLORS = {"low": "green", "medium": "yellow", "high": "red"}


# ------------------------------------------------------------------ مساعدات

def setup_logging(cfg: Config, verbose: bool) -> None:
    cfg.ensure_state_dir()
    handlers: List[logging.Handler] = [logging.FileHandler(cfg.log_path, encoding="utf-8")]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def rich_prompter(request: ApprovalRequest) -> bool:
    color = RISK_COLORS.get(request.risk, "yellow")
    console.print()
    console.print(
        Panel(
            request.summary,
            title=f"[{color}]موافقة مطلوبة — {request.tool} (خطورة: {request.risk})[/{color}]",
            border_style=color,
        )
    )
    if request.details:
        if request.details.lstrip().startswith(("---", "+++", "@@")) or "\n+" in request.details:
            console.print(Syntax(request.details, "diff", theme="ansi_dark", word_wrap=True))
        else:
            console.print(request.details[:4000])
    try:
        return Confirm.ask("[bold]هل تسمح بتنفيذ هذه العملية؟[/bold]", default=False)
    except (EOFError, KeyboardInterrupt):
        console.print("\n[red]أُلغيت العملية.[/red]")
        return False


def build_context(args: argparse.Namespace):
    """يحمّل الإعدادات ويجهّز المخزن والمنفّذ."""
    cfg = load_config(args.root or ".")
    if getattr(args, "model", None):
        cfg.llm["model"] = args.model
    if getattr(args, "sandbox", None):
        cfg.sandbox["mode"] = args.sandbox
    cfg.ensure_state_dir()
    setup_logging(cfg, getattr(args, "verbose", False))

    store = Store(cfg.db_path)
    approvals = ApprovalManager(
        cfg,
        prompter=rich_prompter,
        auto_approve=True if getattr(args, "yes", False) else None,
        deny_all=getattr(args, "dry_run", False),
    )
    executor = Executor(cfg, store, approvals, emit=lambda m: console.print(f"[dim]· {m}[/dim]"))
    return cfg, store, executor


def read_content_arg(args: argparse.Namespace) -> Optional[str]:
    if getattr(args, "stdin", False):
        return sys.stdin.read()
    if getattr(args, "from_file", None):
        source = Path(args.from_file)
        if not source.is_file():
            console.print(f"[red]الملف المصدر غير موجود: {source}[/red]")
            return None
        return source.read_text(encoding="utf-8", errors="replace")
    if getattr(args, "content", None) is not None:
        return args.content
    return None


def print_result(result, success_title: str = "تم") -> int:
    if result.ok:
        console.print(Panel(result.output, title=f"[green]{success_title}[/green]", border_style="green"))
        return 0
    console.print(Panel(result.output, title="[red]فشل[/red]", border_style="red"))
    return 1


# ------------------------------------------------------------------ الأوامر

def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root or ".").resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / CONFIG_FILENAME

    if config_path.exists() and not args.force:
        console.print(f"[yellow]{CONFIG_FILENAME} موجود مسبقاً. استخدم --force لإعادة إنشائه.[/yellow]")
    else:
        write_default_config(root, model=args.model)
        console.print(f"[green]أُنشئ ملف الإعدادات:[/green] {config_path}")

    cfg = load_config(root)
    cfg.ensure_state_dir()
    store = Store(cfg.db_path)
    store.set_meta("initialized_at", str(time.time()))
    store.set_meta("root", str(cfg.root))
    store.close()
    console.print(f"[green]جاهز:[/green] {cfg.state_dir} (قاعدة البيانات + النسخ الاحتياطية + السجل)")

    if not (root / ".git").exists():
        if shutil.which("git") and (args.git or Confirm.ask("تهيئة مستودع Git للتراجع الآمن؟", default=True)):
            subprocess.run(["git", "init", "-q"], cwd=str(root), check=False)
            console.print("[green]تمت تهيئة مستودع Git.[/green]")
    else:
        console.print("[dim]مستودع Git موجود بالفعل.[/dim]")

    console.print("\nالخطوة التالية: [bold]agent analyze[/bold] ثم [bold]agent ask \"...\"[/bold]")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    cfg, store, executor = build_context(args)
    with console.status("[cyan]جارٍ تحليل المشروع...[/cyan]"):
        analysis = analyze_project(cfg)
    store.log_operation("analyze", {"root": str(cfg.root)}, "ok", f"{analysis.file_count} ملف")

    table = Table(title="تحليل المشروع", show_header=True, header_style="bold cyan")
    table.add_column("المؤشر")
    table.add_column("القيمة")
    table.add_row("الجذر", str(cfg.root))
    table.add_row("عدد الملفات", str(analysis.file_count))
    table.add_row("إجمالي الأسطر", str(analysis.total_lines))
    for lang, count in sorted(analysis.languages.items(), key=lambda x: -x[1])[:8]:
        table.add_row(lang, f"{count} ملف / {analysis.language_lines.get(lang, 0)} سطر")
    if analysis.manifests:
        table.add_row("ملفات الإعداد", ", ".join(analysis.manifests))
    if analysis.entry_points:
        table.add_row("نقاط الدخول", ", ".join(analysis.entry_points[:6]))
    detected = detect_test_command(cfg)
    table.add_row("الاختبارات", detected[0] if detected else "غير مكتشفة")
    console.print(table)

    if args.tree:
        console.print(Panel(analysis.tree, title="بنية المجلدات", border_style="cyan"))
    store.close()
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    cfg, store, executor = build_context(args)
    try:
        client = build_client(cfg.llm)
        client.ensure_ready()
    except LLMError as exc:
        console.print(Panel(str(exc), title="[red]النموذج غير جاهز[/red]", border_style="red"))
        store.close()
        return 2

    context_text = None
    if not args.no_context:
        with console.status("[cyan]جارٍ قراءة بنية المشروع...[/cyan]"):
            context_text = analyze_project(cfg).to_text()[:6000]

    def on_step(step: AgentStep) -> None:
        if step.kind == "thought":
            console.print(Panel(step.text, title="[cyan]تفكير النموذج[/cyan]", border_style="cyan"))
        elif step.kind == "tool":
            color = "green" if step.ok else "red"
            args_preview = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(step.args.items())[:4])
            console.print(f"[{color}]▸ أداة:[/{color}] [bold]{step.name}[/bold] ({args_preview})")
            preview = step.text if len(step.text) < 1500 else step.text[:1500] + "\n... [مقتطع]"
            console.print(Panel(preview, border_style=color))
        elif step.kind == "error":
            console.print(f"[red]خطأ: {step.text}[/red]")

    agent = Agent(cfg, store, executor, client=client, on_step=on_step)
    run = agent.run(args.question, project_context=context_text)
    console.print(Panel(run.answer, title="[bold green]الإجابة النهائية[/bold green]", border_style="green"))
    store.close()
    return 0 if run.stopped_reason == "completed" else 1


def cmd_create_file(args: argparse.Namespace) -> int:
    cfg, store, executor = build_context(args)
    content = read_content_arg(args)
    if content is None:
        console.print("[red]حدّد المحتوى عبر --content أو --from-file أو --stdin.[/red]")
        store.close()
        return 1
    result = executor.run("create_file", {"path": args.path, "content": content, "overwrite": args.overwrite})
    code = print_result(result, "أُنشئ الملف")
    store.close()
    return code


def cmd_edit_file(args: argparse.Namespace) -> int:
    cfg, store, executor = build_context(args)
    if args.old is not None:
        if args.new is None:
            console.print("[red]استخدم --new مع --old.[/red]")
            store.close()
            return 1
        result = executor.run(
            "edit_file",
            {"path": args.path, "old_text": args.old, "new_text": args.new, "replace_all": args.all},
        )
    else:
        content = read_content_arg(args)
        if content is None:
            console.print("[red]استخدم --old/--new للاستبدال الجزئي، أو --from-file/--stdin لاستبدال الملف كاملاً.[/red]")
            store.close()
            return 1
        result = executor.run("write_file", {"path": args.path, "content": content})
    code = print_result(result, "تم التعديل")
    store.close()
    return code


def cmd_delete_file(args: argparse.Namespace) -> int:
    cfg, store, executor = build_context(args)
    result = executor.run("delete_file", {"path": args.path})
    code = print_result(result, "تم الحذف")
    store.close()
    return code


def cmd_search(args: argparse.Namespace) -> int:
    cfg, store, executor = build_context(args)
    result = executor.run(
        "search_project",
        {"query": args.query, "regex": args.regex, "include": args.include, "limit": args.limit},
    )
    code = print_result(result, "نتائج البحث")
    store.close()
    return code


def cmd_test(args: argparse.Namespace) -> int:
    cfg, store, executor = build_context(args)
    result = executor.run("run_tests", {"command": args.command, "target": args.target})
    code = print_result(result, "نتائج الاختبارات")
    store.close()
    return code


def cmd_review(args: argparse.Namespace) -> int:
    cfg, store, executor = build_context(args)
    subpath = None
    if args.path:
        try:
            subpath = resolve_in_root(cfg, args.path)
        except SafetyError as exc:
            console.print(f"[red]{exc}[/red]")
            store.close()
            return 1

    with console.status("[cyan]جارٍ فحص الكود...[/cyan]"):
        issues = run_review(cfg, subpath=subpath, max_issues=args.limit)
    counts = summarize_issues(issues)
    store.log_operation("review", {"path": args.path}, "ok", f"{len(issues)} مشكلة")

    if not issues:
        console.print("[green]لم يُعثر على مشاكل وفق القواعد المدمجة.[/green]")
    else:
        table = Table(title=f"نتائج الفحص ({len(issues)} مشكلة)", header_style="bold cyan")
        table.add_column("الخطورة")
        table.add_column("الملف:السطر")
        table.add_column("القاعدة")
        table.add_column("الوصف")
        for issue in issues[: args.limit]:
            table.add_row(
                f"[{RISK_COLORS.get(issue.severity, 'white')}]{issue.severity}[/]",
                f"{issue.path}:{issue.line}",
                issue.rule,
                issue.message,
            )
        console.print(table)
        console.print(
            f"عالية: [red]{counts['high']}[/red] | متوسطة: [yellow]{counts['medium']}[/yellow] | "
            f"منخفضة: [green]{counts['low']}[/green]"
        )
    linters = available_linters()
    if linters:
        console.print(f"[dim]محلّلات خارجية متاحة: {', '.join(linters)}[/dim]")

    if args.llm:
        try:
            client = build_client(cfg.llm)
            client.ensure_ready()
            analysis = analyze_project(cfg).to_text()[:4000]
            issue_text = "\n".join(i.render() for i in issues[:60]) or "لا مشاكل آلية."
            with console.status("[cyan]جارٍ توليد مراجعة بالنموذج...[/cyan]"):
                response = client.chat(
                    [
                        {"role": "system", "content": REVIEW_PROMPT},
                        {"role": "user", "content": f"بنية المشروع:\n{analysis}\n\nنتائج الفحص:\n{issue_text}"},
                    ]
                )
            console.print(Panel(response.content, title="مراجعة النموذج", border_style="cyan"))
        except LLMError as exc:
            console.print(f"[yellow]تعذّرت مراجعة النموذج: {exc}[/yellow]")

    store.close()
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    cfg, store, executor = build_context(args)
    manager = BackupManager(cfg, store)
    restored_any = False

    for _ in range(max(1, args.last)):
        op = store.last_undoable_operation()
        if op is None:
            console.print("[yellow]لا توجد عمليات قابلة للتراجع.[/yellow]")
            break
        rows = store.backups_for(op.id)
        targets = ", ".join(r["rel_path"] for r in rows)
        console.print(
            Panel(
                f"العملية #{op.id} — {op.tool}\nالملفات: {targets}\n"
                f"الوقت: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(op.ts))}",
                title="تراجع",
                border_style="yellow",
            )
        )
        if not (args.yes or Confirm.ask("استرجاع هذه الحالة؟", default=True)):
            console.print("[dim]أُلغي التراجع.[/dim]")
            break
        result = manager.restore_operation(op.id)
        restored_any = True
        if result.restored:
            console.print(f"[green]استُرجع:[/green] {', '.join(result.restored)}")
        if result.deleted:
            console.print(f"[green]حُذف (لأنه أُنشئ في تلك العملية):[/green] {', '.join(result.deleted)}")
        for err in result.errors:
            console.print(f"[red]{err}[/red]")

    if git_available(cfg) and restored_any:
        console.print("[dim]تلميح: راجع النتيجة بـ git diff[/dim]")
    store.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg, store, executor = build_context(args)
    stats = store.stats()

    table = Table(title="حالة الوكيل", header_style="bold cyan")
    table.add_column("العنصر")
    table.add_column("القيمة")
    table.add_row("جذر المشروع", str(cfg.root))
    table.add_row("ملف الإعدادات", str(cfg.source) if cfg.source else "(افتراضي — شغّل agent init)")
    table.add_row("النموذج", f"{cfg.llm['provider']} / {cfg.llm['model']}")
    table.add_row("العزل", str(cfg.sandbox.get("mode")))
    table.add_row("الموافقة التلقائية", "نعم" if cfg.safety.get("auto_approve") else "لا")
    table.add_row("العمليات المسجّلة", str(stats["operations"]))
    table.add_row("الأخطاء", str(stats["errors"]))
    table.add_row("عمليات متراجَع عنها", str(stats["undone"]))

    try:
        client = build_client(cfg.llm)
        alive = client.is_alive()
        table.add_row("Ollama", "[green]متصل[/green]" if alive else "[red]غير متاح[/red]")
    except LLMError as exc:
        table.add_row("Ollama", f"[red]{exc}[/red]")

    table.add_row("Git", "[green]متاح[/green]" if git_available(cfg) else "[yellow]غير مهيّأ[/yellow]")
    table.add_row("Docker", "[green]متاح[/green]" if shutil.which("docker") else "[dim]غير مثبّت[/dim]")
    console.print(table)

    ops = store.recent_operations(limit=args.limit)
    if ops:
        history = Table(title=f"آخر {len(ops)} عملية", header_style="bold cyan")
        history.add_column("#")
        history.add_column("الوقت")
        history.add_column("الأداة")
        history.add_column("الحالة")
        history.add_column("الملخّص")
        for op in ops:
            color = {"ok": "green", "error": "red", "denied": "yellow", "blocked": "red"}.get(op.status, "white")
            summary = (op.summary or "").splitlines()[0][:70] if op.summary else ""
            history.add_row(
                str(op.id),
                time.strftime("%m-%d %H:%M", time.localtime(op.ts)),
                op.tool,
                f"[{color}]{op.status}[/{color}]" + (" (متراجَع)" if op.undone else ""),
                summary,
            )
        console.print(history)

    if git_available(cfg):
        proc = run_git(cfg, ["status", "--short"])
        if proc.stdout.strip():
            console.print(Panel(proc.stdout.strip()[:2000], title="git status", border_style="cyan"))
    store.close()
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    table = Table(title="الأدوات المتاحة للوكيل", header_style="bold cyan")
    table.add_column("الأداة")
    table.add_column("خطورة")
    table.add_column("تعدّل الملفات؟")
    table.add_column("الوصف")
    for tool in all_tools():
        table.add_row(
            tool.name,
            f"[{RISK_COLORS.get(tool.risk, 'white')}]{tool.risk}[/]",
            "نعم" if tool.mutating else "لا",
            tool.description[:90],
        )
    console.print(table)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.root or ".")
    table = Table(title="فحص البيئة", header_style="bold cyan")
    table.add_column("المتطلب")
    table.add_column("الحالة")
    table.add_row("Python", f"{sys.version.split()[0]} " + ("[green]OK[/green]" if sys.version_info >= (3, 10) else "[red]يتطلب 3.10+[/red]"))
    for binary in ("git", "docker", "ollama"):
        found = shutil.which(binary)
        table.add_row(binary, f"[green]{found}[/green]" if found else "[yellow]غير موجود[/yellow]")
    try:
        client = build_client(cfg.llm)
        if client.is_alive():
            models = ", ".join(client.list_models()[:8]) or "لا نماذج"
            table.add_row("خادم Ollama", f"[green]يعمل[/green] — {models}")
        else:
            table.add_row("خادم Ollama", "[red]لا يستجيب — شغّل: ollama serve[/red]")
    except LLMError as exc:
        table.add_row("خادم Ollama", f"[red]{exc}[/red]")
    linters = available_linters()
    table.add_row("محلّلات الكود", ", ".join(linters) if linters else "[dim]لا شيء[/dim]")
    console.print(table)
    return 0



def cmd_image(args: argparse.Namespace) -> int:
    cfg, store, executor = build_context(args)
    with console.status("[cyan]جارٍ تحليل الصورة بنموذج الرؤية...[/cyan]"):
        result = executor.run("analyze_image", {"path": args.path, "prompt": args.prompt or ""})
    code = print_result(result, "تحليل الصورة")
    store.close()
    return code


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        console.print("[red]الخادم يحتاج تبعيات إضافية:[/red] pip install -e \".[server]\"")
        return 1

    from .env_config import load_settings

    settings = load_settings()
    host = args.host or settings.host
    port = args.port or settings.port

    if settings.require_auth and not settings.api_key:
        console.print(Panel(
            "لم يُضبط CODEAGENT_API_KEY — كل الطلبات المحمية سترفض.\n"
            "ولّد مفتاحاً: python -c \"import secrets;print(secrets.token_urlsafe(32))\"\n"
            "ثم ضعه في ملف .env",
            title="[red]تحذير أمني[/red]", border_style="red"))
    if host not in {"127.0.0.1", "localhost", "::1"}:
        console.print("[yellow]تنبيه: الخادم مكشوف على الشبكة. استخدمه خلف HTTPS ومع مفتاح قوي فقط.[/yellow]")

    console.print(f"[green]الواجهة على:[/green] http://{host}:{port}")
    console.print(f"[dim]جذر المشروع: {settings.project_root} | نموذج الرؤية: {settings.vision_model}[/dim]")
    uvicorn.run("server.main:app", host=host, port=port, log_level="info")
    return 0


# ------------------------------------------------------------------ المُحلّل

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="وكيل برمجي محلي يعمل عبر Ollama لقراءة وتعديل وفحص المشاريع البرمجية.",
    )
    parser.add_argument("--version", action="version", version=f"codeagent {__version__}")
    parser.add_argument("--root", help="جذر المشروع (افتراضياً المجلد الحالي)")
    parser.add_argument("--model", help="تجاوز النموذج المحدد في agent.yaml")
    parser.add_argument("--sandbox", choices=["none", "docker"], help="تجاوز وضع العزل")
    parser.add_argument("-y", "--yes", action="store_true", help="الموافقة التلقائية (لا يشمل الحذف والملفات الحساسة)")
    parser.add_argument("--dry-run", action="store_true", help="رفض كل العمليات المعدِّلة (محاكاة فقط)")
    parser.add_argument("-v", "--verbose", action="store_true", help="سجل مفصّل على الشاشة")

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="تهيئة المشروع وإنشاء agent.yaml")
    p_init.add_argument("--force", action="store_true", help="إعادة إنشاء ملف الإعدادات")
    p_init.add_argument("--git", action="store_true", help="تهيئة Git دون سؤال")
    p_init.set_defaults(func=cmd_init)

    p_analyze = sub.add_parser("analyze", help="تحليل بنية المشروع ولغاته")
    p_analyze.add_argument("--tree", action="store_true", help="عرض شجرة المجلدات")
    p_analyze.set_defaults(func=cmd_analyze)

    p_ask = sub.add_parser("ask", help="اطرح مهمة على الوكيل")
    p_ask.add_argument("question", help="السؤال أو المهمة")
    p_ask.add_argument("--no-context", action="store_true", help="عدم إرسال بنية المشروع للنموذج")
    p_ask.set_defaults(func=cmd_ask)

    p_create = sub.add_parser("create-file", help="إنشاء ملف جديد")
    p_create.add_argument("path")
    p_create.add_argument("--content", help="محتوى الملف مباشرة")
    p_create.add_argument("--from-file", dest="from_file", help="قراءة المحتوى من ملف خارجي")
    p_create.add_argument("--stdin", action="store_true", help="قراءة المحتوى من الدخل القياسي")
    p_create.add_argument("--overwrite", action="store_true", help="استبدال الملف إن وُجد")
    p_create.set_defaults(func=cmd_create_file)

    p_edit = sub.add_parser("edit-file", help="تعديل ملف موجود")
    p_edit.add_argument("path")
    p_edit.add_argument("--old", help="النص المراد استبداله")
    p_edit.add_argument("--new", help="النص الجديد")
    p_edit.add_argument("--all", action="store_true", help="استبدال كل التكرارات")
    p_edit.add_argument("--from-file", dest="from_file", help="استبدال الملف كاملاً بمحتوى ملف آخر")
    p_edit.add_argument("--stdin", action="store_true", help="استبدال الملف كاملاً من الدخل القياسي")
    p_edit.set_defaults(func=cmd_edit_file)

    p_delete = sub.add_parser("delete-file", help="حذف ملف (بتأكيد إلزامي)")
    p_delete.add_argument("path")
    p_delete.set_defaults(func=cmd_delete_file)

    p_search = sub.add_parser("search", help="البحث داخل ملفات المشروع")
    p_search.add_argument("query")
    p_search.add_argument("--regex", action="store_true")
    p_search.add_argument("--include", help="نمط glob مثل '*.py'")
    p_search.add_argument("--limit", type=int, default=80)
    p_search.set_defaults(func=cmd_search)

    p_test = sub.add_parser("test", help="تشغيل اختبارات المشروع")
    p_test.add_argument("--command", help="أمر اختبار مخصّص")
    p_test.add_argument("--target", help="ملف أو اختبار محدد")
    p_test.set_defaults(func=cmd_test)

    p_review = sub.add_parser("review", help="فحص الكود واكتشاف المشاكل")
    p_review.add_argument("--path", help="حصر الفحص بمسار")
    p_review.add_argument("--limit", type=int, default=60)
    p_review.add_argument("--llm", action="store_true", help="إضافة مراجعة من النموذج اللغوي")
    p_review.set_defaults(func=cmd_review)

    p_undo = sub.add_parser("undo", help="التراجع عن آخر تعديل")
    p_undo.add_argument("--last", type=int, default=1, help="عدد العمليات المراد التراجع عنها")
    p_undo.set_defaults(func=cmd_undo)

    p_status = sub.add_parser("status", help="حالة الوكيل وسجل العمليات")
    p_status.add_argument("--limit", type=int, default=10)
    p_status.set_defaults(func=cmd_status)

    p_tools = sub.add_parser("tools", help="عرض الأدوات المتاحة")
    p_tools.set_defaults(func=cmd_tools)

    p_image = sub.add_parser("image", help="تحليل صورة بنموذج الرؤية المحلي")
    p_image.add_argument("path", help="مسار الصورة داخل المشروع")
    p_image.add_argument("--prompt", help="سؤال محدد عن الصورة")
    p_image.set_defaults(func=cmd_image)

    p_serve = sub.add_parser("serve", help="تشغيل خادم الويب والواجهة الرسومية")
    p_serve.add_argument("--host", help="عنوان الاستماع (افتراضياً من .env)")
    p_serve.add_argument("--port", type=int, help="المنفذ (افتراضياً من .env)")
    p_serve.set_defaults(func=cmd_serve)

    p_doctor = sub.add_parser("doctor", help="فحص البيئة والمتطلبات")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except SafetyError as exc:
        console.print(Panel(str(exc), title="[red]حظر أمني[/red]", border_style="red"))
        return 3
    except KeyboardInterrupt:
        console.print("\n[yellow]أُوقف التنفيذ بواسطة المستخدم.[/yellow]")
        return 130
    except Exception as exc:  # شبكة أمان أخيرة حتى لا يسقط الوكيل بأثر غامض
        logging.getLogger("codeagent").exception("خطأ غير متوقع")
        console.print(Panel(f"{type(exc).__name__}: {exc}", title="[red]خطأ غير متوقع[/red]", border_style="red"))
        console.print("[dim]راجع .codeagent/agent.log للتفاصيل.[/dim]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
