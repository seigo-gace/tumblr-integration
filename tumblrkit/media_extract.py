"""Bounded, consent-only source fallbacks for a *single* verified Tumblr post.

Not a site crawler, blog scraper or private-content bypass. Extraction is enabled
only for a post already present in the owner-private Discovery store.
"""
import os
from pathlib import Path
import re
import resource
import subprocess
from urllib.parse import urlsplit

MAX_BYTES=128*1024*1024
TIMEOUT=75
VALID_VIDEO={".mp4",".webm",".mov",".m4v",".mkv",".avi"}
BANNED_OUTPUT=("429","rate limit","too many requests","403 forbidden",
               "401 unauthorized","login required","private blog","access denied")
POST_ID=re.compile(r"[0-9]{1,25}\Z")
BLOG=re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")

class ExtractionFailure(ValueError):
    pass

def verified_post_url(url,blog,post_id):
    if (not isinstance(url,str) or len(url)>500 or
            not isinstance(blog,str) or not BLOG.fullmatch(blog) or
            not isinstance(post_id,str) or not POST_ID.fullmatch(post_id)):
        raise ExtractionFailure("MEDIA_POST_INVALID")
    try:
        u=urlsplit(url)
        if (u.scheme!="https" or u.username or u.password or u.port is not None
                or u.fragment or u.query):
            raise ExtractionFailure("MEDIA_POST_INVALID")
        host=(u.hostname or "").lower()
        path=u.path.rstrip("/")
        allowed=(
            host==blog+".tumblr.com" and
            (path=="/post/"+post_id or path.startswith("/post/"+post_id+"/"))
        ) or (
            host in ("www.tumblr.com","tumblr.com") and
            (path=="/"+blog+"/"+post_id or path.startswith("/"+blog+"/"+post_id+"/"))
        )
        if not allowed:
            raise ExtractionFailure("MEDIA_POST_INVALID")
    except ValueError:
        raise ExtractionFailure("MEDIA_POST_INVALID") from None
    return url

def _limit_process():
    resource.setrlimit(resource.RLIMIT_CPU,(65,65))
    resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_BYTES,MAX_BYTES))
    resource.setrlimit(resource.RLIMIT_AS,(1024*1024*1024,1024*1024*1024))

class ExtractorFallback:
    def __init__(self,bin_dir):
        self.bin_dir=Path(bin_dir)

    def _executable(self,name):
        path=self.bin_dir/name
        if (self.bin_dir.is_symlink() or not self.bin_dir.is_dir() or
                self.bin_dir.stat().st_mode & 0o077 or
                not path.is_file() or path.is_symlink() or
                path.stat().st_mode & 0o077 or not os.access(path,os.X_OK)):
            raise ExtractionFailure("MEDIA_EXTRACTOR_UNAVAILABLE")
        return str(path)

    def _run(self,name,url,folder):
        executable=self._executable(name)
        if name=="gallery-dl":
            cmd=[executable,"--config-ignore","--retries","0",
                 "--http-timeout","12","--range","1-3","--post-range","1",
                 "--filesize-max","128M","--limit-rate","2M",
                 "--filter","extension in ('mp4','webm','mov','m4v','mkv','avi')",
                 "--no-mtime","--directory",str(folder),url]
        elif name=="yt-dlp":
            cmd=[executable,"--ignore-config","--no-playlist","--no-progress",
                 "--no-warnings","--no-cache-dir","--no-write-info-json",
                 "--no-write-thumbnail","--no-embed-metadata",
                 "--retries","0","--fragment-retries","0",
                 "--extractor-retries","0","--socket-timeout","12",
                 "--max-downloads","1","--max-filesize","128M",
                 "--limit-rate","2M","--no-continue","--no-part",
                 "--paths",str(folder),"-o","selected.%(ext)s",url]
        else:
            raise ExtractionFailure("MEDIA_EXTRACTOR_INVALID")
        env={"PATH":"/usr/bin:/bin","LANG":"C.UTF-8",
             "HOME":str(folder),"XDG_CONFIG_HOME":str(folder)}
        try:
            outcome=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                  timeout=TIMEOUT,check=False,env=env,cwd=folder,
                                  preexec_fn=_limit_process)
        except (OSError,subprocess.TimeoutExpired):
            raise ExtractionFailure("MEDIA_EXTRACTOR_FAILED") from None
        diagnostic=(outcome.stderr+outcome.stdout)[-6000:].decode("utf-8","replace").lower()
        if any(x in diagnostic for x in BANNED_OUTPUT):
            raise ExtractionFailure("MEDIA_PROVIDER_REFUSED")
        if outcome.returncode:
            raise ExtractionFailure("MEDIA_EXTRACTOR_NOT_FOUND")
        files=[]
        for path in folder.rglob("*"):
            if path.is_symlink():
                raise ExtractionFailure("MEDIA_EXTRACTOR_UNSAFE_FILE")
            if path.is_file():
                if not path.resolve().is_relative_to(folder.resolve()):
                    raise ExtractionFailure("MEDIA_EXTRACTOR_UNSAFE_FILE")
                if path.suffix.lower() in VALID_VIDEO and 0<path.stat().st_size<=MAX_BYTES:
                    files.append(path)
                elif path.stat().st_size>MAX_BYTES:
                    raise ExtractionFailure("MEDIA_SIZE_EXCEEDED")
        if not files:
            raise ExtractionFailure("MEDIA_EXTRACTOR_NOT_FOUND")
        # One post, one video. Never silently select an arbitrary video from a collection.
        if len(files)!=1:
            raise ExtractionFailure("MEDIA_EXTRACTOR_AMBIGUOUS")
        return files[0]

    def fetch(self,post_url,blog,post_id,destination):
        verified_post_url(post_url,blog,post_id)
        parent=Path(destination).parent
        for name in ("gallery-dl","yt-dlp"):
            work=parent/(".extract-"+name)
            work.mkdir(mode=0o700)
            try:
                selected=self._run(name,post_url,work)
                size=selected.stat().st_size
                if not 0<size<=MAX_BYTES:
                    raise ExtractionFailure("MEDIA_SIZE_EXCEEDED")
                os.replace(selected,destination)
                os.chmod(destination,0o600)
                return {"bytes":size,"method":name}
            except ExtractionFailure as exc:
                if str(exc) in ("MEDIA_PROVIDER_REFUSED","MEDIA_SIZE_EXCEEDED",
                                "MEDIA_EXTRACTOR_AMBIGUOUS","MEDIA_EXTRACTOR_UNSAFE_FILE"):
                    raise
                # Only normal unsupported/not-found outcomes permit the next
                # provider-specific extractor. No auth, rate-limit or bypass retry.
            finally:
                import shutil
                shutil.rmtree(work,ignore_errors=True)
        raise ExtractionFailure("MEDIA_ALL_METHODS_EXHAUSTED")
