"""Installer operations shared by the GTK app, CLI, and tests."""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

RECOMMENDED = 'GE-Proton11-11'
CABINET_URL = 'https://msdl.microsoft.com/download/symbols/cabinet.dll/2F2BCD442a000/cabinet.dll'
CABINET_SHA256 = '260b0a755db14cf01bccb7cae1da15feb1b25eb2237f126a469e12a940441571'
VC_URL = 'https://aka.ms/vc14/vc_redist.x64.exe'
WEBVIEW_URL = 'https://go.microsoft.com/fwlink/?linkid=2124701'
HELPERS = ('roblox-compression.py', 'cursor-visibility.py', 'prepare-proton.py', 'lynrt.dll', 'cursor-state.exe')


# The frozen runtime's OpenSSL was built on Ubuntu and only looks in
# /usr/lib/ssl, which Arch, Fedora, openSUSE etc. lack. Find the host bundle.
CA_BUNDLES = (
    '/etc/ssl/certs/ca-certificates.crt',  # Debian/Ubuntu/Arch/Gentoo
    '/etc/pki/tls/certs/ca-bundle.crt',    # Fedora/RHEL
    '/etc/ssl/ca-bundle.pem',              # openSUSE
    '/etc/ssl/cert.pem',                   # Arch/Alpine/macOS-style
    '/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem',
)


def ssl_context():
    context = ssl.create_default_context()
    for name in ('SSL_CERT_FILE', 'SSL_CERT_DIR'):
        if os.environ.get(name):
            return context
    for bundle in CA_BUNDLES:
        if os.path.isfile(bundle):
            try:
                context.load_verify_locations(cafile=bundle)
                break
            except (OSError, ssl.SSLError):
                continue
    if os.path.isdir('/etc/ssl/certs'):
        context.load_verify_locations(capath='/etc/ssl/certs')
    return context


class Cancelled(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Layout:
    home: Path
    data: Path
    config: Path
    bin: Path
    applications: Path
    icons: Path

    @classmethod
    def current(cls):
        home = Path.home()
        share = Path(os.environ.get('XDG_DATA_HOME', home / '.local/share'))
        config = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config'))
        return cls(home, Path(os.environ.get('LYNIX_DATA_DIR', share/'lynix-proton')),
                   Path(os.environ.get('LYNIX_CONFIG_DIR', config/'lynix-proton')),
                   home/'.local/bin', share/'applications', share/'icons/hicolor/scalable/apps')

    @classmethod
    def isolated(cls, root):
        """Install under a test root without touching the user's shortcuts/settings."""
        root = Path(root).resolve()
        return cls(root, root/'data', root/'config', root/'bin', root/'applications', root/'icons')


def payload_root():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)/'payload'
    source = Path(__file__).resolve().parents[1]
    prepared = source/'.build/appimage/payload'
    return prepared if (prepared/'build-id').exists() else source


def clean_environment():
    """Don't pass the AppImage's bundled libraries into Proton or host tools."""
    env = dict(os.environ)
    if getattr(sys, 'frozen', False):
        original = env.pop('LD_LIBRARY_PATH_ORIG', None)
        if original:
            env['LD_LIBRARY_PATH'] = original
        else:
            env.pop('LD_LIBRARY_PATH', None)
        for key in ('PYTHONHOME', 'PYTHONPATH', 'GI_TYPELIB_PATH', 'GTK_PATH',
                    'GDK_PIXBUF_MODULE_FILE', 'GDK_PIXBUF_MODULEDIR', 'GIO_MODULE_DIR',
                    'GTK_EXE_PREFIX', 'GTK_DATA_PREFIX', 'XDG_DATA_DIRS'):
            env.pop(key, None)
    return env


def read_config(path):
    result = {}
    try:
        for line in path.read_text().splitlines():
            key, sep, value = line.partition('=')
            if sep and key in ('EXE_PATH', 'PROTON_DIR'):
                result[key] = value
    except OSError:
        pass
    return result


def valid_exe(path):
    try:
        with Path(path).open('rb') as stream:
            return stream.read(2) == b'MZ'
    except (OSError, ValueError):
        return False


def valid_proton(path):
    p = Path(path)
    return (p/'proton').is_file() and os.access(p/'proton', os.X_OK) and (p/'files').is_dir()


def proton_name(path):
    try:
        text = (Path(path)/'version').read_text().strip()
        first, _, rest = text.partition(' ')
        return rest if first.isdecimal() and rest else text
    except OSError:
        return Path(path).name


def steam_roots(home):
    roots = [home/'.local/share/Steam', home/'.steam/steam', home/'.steam/root',
             home/'.var/app/com.valvesoftware.Steam/data/Steam']
    for root in list(roots):
        try:
            text = (root/'steamapps/libraryfolders.vdf').read_text()
            for value in re.findall(r'"path"\s*"((?:\\.|[^"\\])*)"', text):
                roots.append(Path(value.replace('\\\\', '\\').replace('\\"', '"')))
        except OSError:
            pass
    return list(dict.fromkeys(p.resolve() for p in roots))


def discover_proton(layout):
    bases = [layout.data/'proton']
    for root in steam_roots(layout.home):
        bases.extend((root/'compatibilitytools.d', root/'steamapps/common'))
    bases.extend((Path('/usr/share/steam/compatibilitytools.d'), Path('/usr/local/share/steam/compatibilitytools.d')))
    found = []
    config = read_config(layout.config/'config')
    if config.get('PROTON_DIR') and valid_proton(config['PROTON_DIR']):
        found.append(Path(config['PROTON_DIR']).resolve())
    for base in bases:
        if base.is_dir():
            found.extend(p.resolve() for p in base.iterdir() if valid_proton(p))
    return sorted(set(found), key=lambda p: (proton_name(p) != RECOMMENDED, proton_name(p).casefold(), str(p)))


def has_prefix(path):
    return (path/'pfx/drive_c').is_dir() and (path/'pfx/system.reg').is_file()


def detect_prefix(layout):
    if os.environ.get('LYNIX_PREFIX'):
        return Path(os.environ['LYNIX_PREFIX']).expanduser().resolve()
    try:
        saved = json.loads((layout.config/'setup.json').read_text())
        if saved.get('prefix') and has_prefix(Path(saved['prefix'])):
            return Path(saved['prefix'])
    except (OSError, ValueError):
        pass
    # Migrate the original standalone entry point; read its literal path only.
    try:
        text = (layout.bin/'lynix').read_text()
        match = re.search(r'^STEAM_PREFIX_DEFAULT="([^"\n]+)"$', text, re.M)
        if match and has_prefix(Path(match[1])):
            return Path(match[1]).resolve()
    except OSError:
        pass
    private = layout.data/'prefix'
    if has_prefix(private):
        return private
    candidates = []
    if (layout.config/'config').is_file():
        for root in steam_roots(layout.home):
            compatdata = root/'steamapps/compatdata'
            if compatdata.is_dir():
                for p in compatdata.iterdir():
                    local = p/'pfx/drive_c/users/steamuser/AppData/Local'
                    if has_prefix(p) and ((p/'pfx/drive_c/Lynx').is_dir() or (local/'com.savage.lynx').is_dir()):
                        candidates.append(p.resolve())
    return candidates[0] if len(set(candidates)) == 1 else private


def vc_present(prefix):
    try:
        text = (prefix/'pfx/system.reg').read_text(errors='replace')
        key = r'[Software\\Microsoft\\VisualStudio\\14.0\\VC\\Runtimes\\x64]'
        section = text.split(key, 1)[1].split('\n[', 1)[0]
        version = re.search(r'"Version"="v?(\d+)\.(\d+)', section)
        return bool(version and tuple(map(int, version.groups())) >= (14, 40))
    except (OSError, IndexError):
        return False


def webview_present(prefix):
    for folder in ('Program Files (x86)', 'Program Files'):
        root = prefix/f'pfx/drive_c/{folder}/Microsoft/EdgeWebView/Application'
        if root.exists() and next(root.glob('*/msedgewebview2.exe'), None):
            return True
    return False


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.lynix-', delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            tmp.chmod(mode)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def quote_desktop(path):
    return '"'+str(path).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')+'"'


class Installer:
    def __init__(self, layout=None, payload=None, progress=None, cancel=None):
        self.layout = layout or Layout.current()
        self.payload = Path(payload) if payload else payload_root()
        self.progress = progress or (lambda value, text: None)
        self.cancel = cancel or threading.Event()
        self.log = self.layout.data/'logs/setup.log'
        self.launcher = self.layout.bin/'lynix'

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled('Setup cancelled. You can run it again to finish.')

    def note(self, value, text):
        self.check_cancel()
        self.progress(value, text)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open('a') as f:
            f.write(time.strftime('[%Y-%m-%d %H:%M:%S] ')+text+'\n')

    def save(self, path, data, mode=0o644):
        if path.exists() and path.read_bytes() == data:
            path.chmod(mode)
            return
        if path.exists():
            old = path.read_bytes()
            backup = self.layout.data/'backups/setup'/f'{path.name}.{hashlib.sha256(old).hexdigest()[:16]}'
            if not backup.exists():
                atomic_write(backup, old)
        atomic_write(path, data, mode)

    def download(self, url, destination, start, end, label, sha=None, minimum=1):
        if destination.is_file() and destination.stat().st_size >= minimum and (not sha or checksum(destination) == sha):
            self.note(end, f'{label} is already downloaded')
            return destination.read_bytes()
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix+'.part')
        for attempt in range(3):
            try:
                self.check_cancel()
                request = urllib.request.Request(url, headers={'User-Agent': 'Lynix-patch-setup/0.1'})
                with urllib.request.urlopen(request, timeout=30, context=ssl_context()) as response, partial.open('wb') as stream:
                    total = int(response.headers.get('Content-Length', 0))
                    done = 0
                    while True:
                        self.check_cancel()
                        chunk = response.read(256*1024)
                        if not chunk:
                            break
                        stream.write(chunk)
                        done += len(chunk)
                        ratio = min(done/total, 1) if total else 0
                        self.progress(start+(end-start)*ratio, f'Downloading {label}… {done/1048576:.1f} MB')
                if partial.stat().st_size < minimum:
                    raise ValueError(f'{label} download is incomplete')
                if sha and checksum(partial) != sha:
                    raise ValueError(f'{label} checksum did not match; no DLL was installed')
                os.replace(partial, destination)
                return destination.read_bytes()
            except Cancelled:
                raise
            except (OSError, urllib.error.URLError) as exc:
                if attempt == 2:
                    raise RuntimeError(f'Could not download {label}. Check your connection and retry.\n{exc}') from exc
                if self.cancel.wait(.5*(attempt+1)):
                    self.check_cancel()
            finally:
                partial.unlink(missing_ok=True)
        raise RuntimeError('Download did not complete')

    def install_support(self):
        """Install a persistent runtime: moving/removing the AppImage is harmless."""
        if getattr(sys, 'frozen', False):
            source = Path(sys.executable).parent
            target = self.layout.data/'runtime'
            marker = (source/'_internal/payload/build-id').read_text().strip()
            current = target/'_internal/payload/build-id'
            if not current.exists() or current.read_text().strip() != marker:
                self.note(.08, 'Installing the launcher support files…')
                staging = Path(tempfile.mkdtemp(prefix='.runtime-', dir=self.layout.data))
                try:
                    shutil.copytree(source, staging, dirs_exist_ok=True, symlinks=True)
                    old = self.layout.data/'.runtime-previous'
                    if old.exists():
                        shutil.rmtree(old)
                    if target.exists():
                        os.replace(target, old)
                    os.replace(staging, target)
                    if old.exists():
                        shutil.rmtree(old)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
        else:
            # Developer runs use the system Python; the AppImage bundles its own.
            if not shutil.which('python3'):
                raise RuntimeError('Python 3 is required for a source install')
        for name in HELPERS:
            source = self.payload/'compat'/name
            self.save(self.layout.data/'compat'/name, source.read_bytes())
        self.save(self.layout.bin/'lynix-proton.sh', (self.payload/'lynix-proton.sh').read_bytes(), 0o755)

    def install_cabinet(self):
        compat = self.layout.data/'compat'
        cabinet = compat/'cabinet.dll'
        # Keep the proven local DLL (including the ISO-supplied variant) on upgrades.
        try:
            manifest = json.loads((compat/'manifest.json').read_text())
            data = cabinet.read_bytes()
            existing = manifest.get('cabinet.dll') == hashlib.sha256(data).hexdigest() and data.count(b'lynrt.dll\0') == 1
        except (OSError, ValueError):
            existing = False
        if existing:
            self.note(.28, 'The compression fix is already installed')
        else:
            original = self.download(CABINET_URL, self.layout.data/'cache/cabinet-microsoft.dll', .16,.28,
                                     'Microsoft compression DLL', CABINET_SHA256, 100000)
            if original.count(b'ntdll.dll\0') != 1:
                raise ValueError('The Microsoft DLL has an unexpected import table')
            self.save(self.layout.data/'backups/cabinet-unmodified.dll', original)
            self.save(cabinet, original.replace(b'ntdll.dll\0', b'lynrt.dll\0'))
        manifest = {name: checksum(compat/name) for name in ('cabinet.dll','lynrt.dll')}
        self.save(compat/'manifest.json', (json.dumps(manifest, indent=2)+'\n').encode())

    def run_wrapper(self, exe, proton, prefix):
        env = clean_environment()
        env.update(LYNIX_PREFIX=str(prefix), LYNIX_DATA_DIR=str(self.layout.data),
                   LYNIX_CONFIG_DIR=str(self.layout.config), LYNIX_SETUP_PROGRESS='1')
        # An explicit selection takes priority over inherited opt-in settings.
        env.pop('LYNIX_PROTON', None)
        command = ['bash', str(self.layout.bin/'lynix-proton.sh'), '--setup', '--no-gui',
                   '--exe', str(exe), '--proton', str(proton)]
        proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        sel = None
        try:
            sel = selectors.DefaultSelector()
            sel.register(proc.stdout, selectors.EVENT_READ)
            buffer = b''
            with self.log.open('ab') as log:
                while True:
                    self.check_cancel()
                    if sel.select(.2):
                        block = os.read(proc.stdout.fileno(), 65536)
                        if not block:
                            break
                        log.write(block);log.flush()
                        buffer += block
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            if line.startswith(b'LYNIX_PROGRESS|'):
                                _, value, text = line.decode(errors='replace').split('|', 2)
                                self.progress(float(value)/100, text)
                    elif proc.poll() is not None:
                        break
                code = proc.wait()
            if code:
                raise RuntimeError(f'Runtime setup did not complete (exit {code}).\nSee {self.log}')
        finally:
            if sel is not None:
                sel.close()
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:proc.wait(timeout=3)
                except subprocess.TimeoutExpired:os.killpg(proc.pid, signal.SIGKILL);proc.wait()

    def desktop(self, prefix):
        l = self.layout
        text = '#!/usr/bin/env bash\nset -euo pipefail\n'
        for name, value in (('LYNIX_PREFIX',prefix),('LYNIX_DATA_DIR',l.data),('LYNIX_CONFIG_DIR',l.config)):
            text += f'export {name}={shlex.quote(str(value))}\n'
        text += f'exec {shlex.quote(str(l.bin/"lynix-proton.sh"))} "$@"\n'
        self.save(self.launcher, text.encode(), 0o755)
        icon = l.icons/'lynix-patch.svg'
        self.save(icon, (self.payload/'setup/assets/lynix-patch.svg').read_bytes())
        entry = f'''[Desktop Entry]
Type=Application
Version=1.0
Name=Lynx
Comment=Launch Lynx with Lynix-patch
Exec={quote_desktop(self.launcher)}
TryExec={l.bin/'lynix'}
Icon={icon}
Terminal=false
Categories=Game;Utility;
Keywords=lynx;lynix;roblox;proton;
StartupNotify=true
StartupWMClass=steam_proton
'''
        self.save(l.applications/'lynx.desktop', entry.encode(), 0o755)
        if shutil.which('update-desktop-database'):
            subprocess.run(['update-desktop-database',str(l.applications)], env=clean_environment(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    def install(self, exe, proton, prefix=None):
        exe = Path(exe).expanduser().resolve()
        proton = Path(proton).expanduser().resolve()
        if '\n' in str(exe)+str(proton) or '\r' in str(exe)+str(proton):
            raise ValueError('Paths containing newlines are not supported')
        if not valid_exe(exe):
            raise ValueError('Choose a readable Windows UI.exe file')
        if not valid_proton(proton):
            raise ValueError('Choose a Proton folder containing the proton executable and files folder')
        prefix = Path(prefix).resolve() if prefix else detect_prefix(self.layout)
        self.layout.data.mkdir(parents=True, exist_ok=True)
        self.layout.config.mkdir(parents=True, exist_ok=True)
        with (self.layout.data/'.setup.lock').open('a') as lock:
            try:fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('Another Lynix-patch setup is already running')
            self.note(.02, 'Checking your installation…')
            self.install_support()
            self.install_cabinet()
            if vc_present(prefix):
                self.note(.36, 'Visual C++ is already installed — skipping')
            else:
                self.download(VC_URL, self.layout.data/'cache/vc_redist.x64.exe',.28,.36,'Visual C++ runtime',minimum=5*1024*1024)
            if webview_present(prefix):
                self.note(.55, 'WebView2 is already installed — skipping')
            else:
                self.download(WEBVIEW_URL,self.layout.data/'cache/MicrosoftEdgeWebView2RuntimeInstallerX64.exe',.36,.55,'WebView2 runtime',minimum=100*1024*1024)
            self.note(.56, 'Configuring the Proton prefix…')
            self.run_wrapper(exe,proton,prefix)
            # Apply to existing game versions without starting any background process.
            spec=importlib.util.spec_from_file_location('roblox_compression', self.layout.data/'compat/roblox-compression.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            assets={name:(self.layout.data/'compat'/name).read_bytes() for name in ('cabinet.dll','lynrt.dll')}
            module.repair(prefix/'pfx/drive_c/users/steamuser/AppData/Local/Roblox/Versions',assets,self.layout.data/'backups/roblox-compression')
            self.note(.95, 'Creating the Lynx shortcut…')
            self.desktop(prefix)
            self.save(self.layout.config/'setup.json',(json.dumps({'prefix':str(prefix),'ui_exe':str(exe),'proton':str(proton)},indent=2)+'\n').encode(),0o600)
            self.note(1., 'Ready — launching Lynx…')
        return self.launcher

    def launch(self):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with (self.log.parent/'desktop-launch.log').open('ab') as log:
            return subprocess.Popen([str(self.launcher)],env=clean_environment(),stdin=subprocess.DEVNULL,
                                    stdout=log,stderr=log,start_new_session=True)
