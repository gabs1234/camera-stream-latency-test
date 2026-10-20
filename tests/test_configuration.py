import sys

import pytest

from camera_latency.__main__ import main
import camera_latency.receiver


@pytest.fixture
def configured_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ('CAMERA_URL', 'CAMERA_USERNAME', 'CAMERA_PASSWORD', 'CUSTOM_PASSWORD'):
        monkeypatch.delenv(name, raising=False)
    captured = []
    monkeypatch.setattr(camera_latency.receiver, 'measure', captured.append)
    monkeypatch.setattr('getpass.getpass', lambda _: 'prompted-password')
    def run(*args):
        monkeypatch.setattr(sys, 'argv', ['camera_latency', 'measure', *args])
        assert main() == 0
        return captured[-1]
    return tmp_path, run


def test_dotenv_stream_credentials_are_literal_and_not_logged(configured_cli, capsys):
    directory, run = configured_cli
    (directory / '.env').write_text("CAMERA_URL='https://camera.test/stream'\n"
                                    "CAMERA_USERNAME='viewer'\n"
                                    "CAMERA_PASSWORD='secret # ${LITERAL} $value'\n")
    args = run()
    assert args.url == 'https://camera.test/stream'
    assert args.username == 'viewer'
    assert args.password == 'secret # ${LITERAL} $value'
    output = capsys.readouterr()
    assert not output.out and not output.err


def test_cli_overrides_exported_environment_overrides_dotenv(configured_cli, monkeypatch):
    directory, run = configured_cli
    (directory / '.env').write_text('CAMERA_URL=https://file.test/stream\n'
                                    'CAMERA_USERNAME=file-user\nCAMERA_PASSWORD=file-password\n')
    monkeypatch.setenv('CAMERA_URL', 'https://env.test/stream')
    monkeypatch.setenv('CAMERA_USERNAME', 'env-user')
    monkeypatch.setenv('CAMERA_PASSWORD', 'env-password')
    args = run()
    assert (args.url, args.username, args.password) == ('https://env.test/stream', 'env-user', 'env-password')
    args = run('--url', 'https://cli.test/stream', '--username', 'cli-user')
    assert (args.url, args.username, args.password) == ('https://cli.test/stream', 'cli-user', 'env-password')


def test_explicit_password_variable_wins(configured_cli):
    directory, run = configured_cli
    (directory / '.env').write_text('CAMERA_URL=https://camera.test/stream\n'
                                    'CAMERA_USERNAME=viewer\nCAMERA_PASSWORD=default\nCUSTOM_PASSWORD=custom\n')
    assert run('--password-env', 'CUSTOM_PASSWORD').password == 'custom'


def test_blank_default_password_prompts_and_cli_without_dotenv_still_works(configured_cli):
    directory, run = configured_cli
    args = run('--url', 'https://camera.test/stream', '--username', 'viewer')
    assert args.password == 'prompted-password'
    (directory / '.env').write_text('CAMERA_PASSWORD=\n')
    assert run('--url', 'https://camera.test/stream', '--username', 'viewer').password == 'prompted-password'


def test_missing_url_reports_configuration_requirement(configured_cli, capsys):
    _, run = configured_cli
    with pytest.raises(SystemExit) as exc:
        run()
    assert exc.value.code == 2
    assert 'CAMERA_URL' in capsys.readouterr().err
