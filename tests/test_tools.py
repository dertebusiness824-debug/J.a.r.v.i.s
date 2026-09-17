from jarvis.tools import (
    calculate_expression,
    get_current_time,
    list_directory,
    read_file,
    run_terminal,
    write_file,
)


def test_calculate_expression_integer():
    assert calculate_expression.invoke({"expression": "17 * 24"}) == "408"


def test_calculate_expression_rejects_calls():
    out = calculate_expression.invoke({"expression": "__import__('os').system('pwd')"})
    assert out.startswith("Error")


def test_get_current_time_utc():
    stamp = get_current_time.invoke({"timezone_name": "UTC"})
    assert "T" in stamp


def test_sandbox_write_read_and_list(tmp_path, monkeypatch):
    write_file.invoke({"path": "hello.txt", "content": "jarvis"})
    assert read_file.invoke({"path": "hello.txt"}) == "jarvis"
    listing = list_directory.invoke({"path": "."})
    assert "hello.txt" in listing


def test_sandbox_blocks_path_traversal():
    out = read_file.invoke({"path": "../../etc/passwd"})
    assert out.startswith("Error")
    assert "sandbox" in out.lower() or "fuera" in out.lower()


def test_terminal_blocks_destructive_command():
    out = run_terminal.invoke({"command": "rm -rf /"})
    assert "bloqueado" in out.lower()


def test_terminal_echo():
    out = run_terminal.invoke({"command": "echo jarvis-ok"})
    assert "jarvis-ok" in out
