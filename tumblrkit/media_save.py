"""Owner-confirmed Tumblr Media Save worker; no scraping, no provider API calls.

State lives alongside Discovery/Library in its existing private SQLite database.
Only one explicitly authorized Tumblr-hosted video URL can be fetched per action.
No yt-dlp extraction of inaccessible sources, DRM bypass, retry loop, or auto job.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import resource
import shutil
import socket
import sqlite3
import ssl
import subprocess
import threading
import time
import uuid
from contextlib import closing
from urllib.parse import urlsplit

from tumblrkit.discovery_store import DiscoveryStore
from tumblrkit.library import _safe_media, IDENT
from tumblrkit.media_extract import ExtractorFallback, ExtractionFailure, verified_post_url
from tumblrkit.api_safety import TumblrReadBudget

MAX_SOURCE_BYTES=128*1024*1024
MAX_OUTPUT_BYTES=256*1024*1024
MIN_DISK_FREE_BYTES=512*1024*1024
MAX_TOTAL_SAVED_BYTES=1024*1024*1024
MAX_JOBS=1
CONVERT_TIMEOUT=180
HTTP_TIMEOUT=12
MEDIA_HOST=re.compile(r"(?:[a-z0-9-]+\.)?media\.tumblr\.com\Z")
KEY=re.compile(r"[0-9a-f]{32}\Z")
SCHEMA="""
CREATE TABLE IF NOT EXISTS media_save_jobs(
 id TEXT PRIMARY KEY,
 candidate_id TEXT NOT NULL,
 source_url TEXT NOT NULL,
 source_post_id TEXT NOT NULL,
 media_index INTEGER NOT NULL,
 state TEXT NOT NULL,
 error_code TEXT NOT NULL DEFAULT '',
 source_bytes INTEGER NOT NULL DEFAULT 0,
 output_bytes INTEGER NOT NULL DEFAULT 0,
 output_name TEXT NOT NULL DEFAULT '',
 source_method TEXT NOT NULL DEFAULT '',
 sha256 TEXT NOT NULL DEFAULT '',
 created_at INTEGER NOT NULL,
 updated_at INTEGER NOT NULL,
 UNIQUE(candidate_id,media_index,source_url),
 FOREIGN KEY(candidate_id) REFERENCES discovery_posts(candidate_id)
);
"""

class MediaError(ValueError):
    pass

def _media_url(value):
    if not isinstance(value,str) or len(value)>500:
        raise MediaError("MEDIA_URL_INVALID")
    try:
        u=urlsplit(value)
        if (u.scheme!="https" or u.username or u.password or u.port is not None
                or not MEDIA_HOST.fullmatch(u.hostname or "")
                or not u.path.startswith("/") or any(ord(x)<32 for x in value)
                or u.fragment):
            raise MediaError("MEDIA_URL_INVALID")
    except ValueError:
        raise MediaError("MEDIA_URL_INVALID") from None
    return u

class PinnedHTTPS(http.client.HTTPSConnection):
    """Direct TLS connection to a checked public address; no re-resolution or redirect."""
    def connect(self):
        infos=socket.getaddrinfo(self.host,443,type=socket.SOCK_STREAM)
        candidates=[]
        for family,_,_,_,addr in infos:
            try:
                ip=ipaddress.ip_address(addr[0])
            except ValueError:
                continue
            if ip.is_global:
                candidates.append((family,str(ip)))
        if not candidates:
            raise MediaError("MEDIA_DESTINATION_UNSAFE")
        last=None
        for family,address in candidates:
            sock=socket.socket(family,socket.SOCK_STREAM)
            sock.settimeout(HTTP_TIMEOUT)
            try:
                if family==socket.AF_INET6:
                    sock.connect((address,443,0,0))
                else:
                    sock.connect((address,443))
                self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
                return
            except (OSError,ssl.SSLError) as exc:
                last=exc
                sock.close()
        raise MediaError("MEDIA_CONNECT_UNAVAILABLE") from last

class SafeDownloader:
    def _call(self,url,method):
        parsed=_media_url(url)
        conn=PinnedHTTPS(parsed.hostname,timeout=HTTP_TIMEOUT,context=ssl.create_default_context())
        path=parsed.path+("?" + parsed.query if parsed.query else "")
        conn.request(method,path,headers={
            "Accept":"video/mp4,video/webm,video/quicktime,application/octet-stream",
            "User-Agent":"TGtumbler-MediaSave/1.0",
            "Connection":"close",
        })
        response=conn.getresponse()
        if response.status!=200:
            code=response.status
            conn.close()
            if code in (401,403,429):
                raise MediaError("MEDIA_PROVIDER_REFUSED")
            if code==405 and method=="HEAD":
                raise MediaError("MEDIA_HEAD_UNSUPPORTED")
            raise MediaError("MEDIA_HTTP_UNAVAILABLE")
        kind=response.getheader("Content-Type","").split(";",1)[0].strip().lower()
        if kind not in ("video/mp4","video/webm","video/quicktime","application/octet-stream"):
            conn.close()
            raise MediaError("MEDIA_TYPE_UNSUPPORTED")
        length=response.getheader("Content-Length")
        if not length or not length.isascii() or not length.isdecimal():
            conn.close()
            raise MediaError("MEDIA_LENGTH_UNKNOWN")
        size=int(length)
        if size<=0 or size>MAX_SOURCE_BYTES:
            conn.close()
            raise MediaError("MEDIA_SIZE_EXCEEDED")
        return conn,response,size

    def probe(self,url):
        conn,response,size=self._call(url,"HEAD")
        try:
            response.read()
        finally:
            conn.close()
        return size

    def fetch(self,url,destination,expected_size):
        conn,response,size=self._call(url,"GET")
        if size!=expected_size:
            conn.close()
            raise MediaError("MEDIA_SIZE_CHANGED")
        downloaded=0
        try:
            with open(destination,"xb") as file:
                while True:
                    chunk=response.read(min(65536,MAX_SOURCE_BYTES+1-downloaded))
                    if not chunk:
                        break
                    downloaded+=len(chunk)
                    if downloaded>expected_size or downloaded>MAX_SOURCE_BYTES:
                        raise MediaError("MEDIA_SIZE_EXCEEDED")
                    file.write(chunk)
                file.flush()
                os.fsync(file.fileno())
        finally:
            conn.close()
        if downloaded!=expected_size:
            raise MediaError("MEDIA_SIZE_CHANGED")
        return downloaded

def _bound_child():
    """Limit FFmpeg/ffprobe even for malformed, compressed or hostile inputs."""
    resource.setrlimit(resource.RLIMIT_CPU,(190,190))
    resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_OUTPUT_BYTES,MAX_OUTPUT_BYTES))
    resource.setrlimit(resource.RLIMIT_AS,(2*1024*1024*1024,2*1024*1024*1024))

class Transcoder:
    def __init__(self,ffprobe="ffprobe",ffmpeg="ffmpeg"):
        self.ffprobe=ffprobe
        self.ffmpeg=ffmpeg

    def _probe(self,source):
        try:
            result=subprocess.run([self.ffprobe,"-v","error","-protocol_whitelist","file,pipe","-show_entries",
                 "format=format_name,duration:stream=codec_type,codec_name,width,height",
                 "-of","json",str(source)],capture_output=True,text=True,timeout=20,check=False,
                                  preexec_fn=_bound_child)
        except (OSError,subprocess.TimeoutExpired):
            raise MediaError("MEDIA_PROBE_UNAVAILABLE") from None
        if result.returncode or len(result.stdout)>16000:
            raise MediaError("MEDIA_PROBE_FAILED")
        try:
            data=json.loads(result.stdout)
            streams=data["streams"]
            video=[s for s in streams if s.get("codec_type")=="video"]
            duration=float(data.get("format",{}).get("duration","0"))
            if (not video or not 0<duration<=3600
                or not 0<int(video[0].get("width",0))<=3840
                or not 0<int(video[0].get("height",0))<=2160):
                raise MediaError("MEDIA_PROBE_FAILED")
        except (ValueError,KeyError,TypeError,OverflowError):
            raise MediaError("MEDIA_PROBE_FAILED") from None
        return {"video":video[0]["codec_name"],
                "audio":[s["codec_name"] for s in streams if s.get("codec_type")=="audio"],
                "format":data.get("format",{}).get("format_name","")}

    def convert(self,source,destination):
        before=self._probe(source)
        # All outputs are MP4. Only verified compatible tracks may be stream-copied.
        compatible=before["video"]=="h264" and all(x=="aac" for x in before["audio"])
        args=[self.ffmpeg,"-hide_banner","-nostdin","-loglevel","error","-y",
              "-threads","2","-protocol_whitelist","file,pipe","-i",str(source),"-map","0:v:0","-map","0:a:0?",
              "-c:v","copy" if compatible else "libx264"]
        if not compatible:
            args+=["-preset","veryfast","-crf","24","-pix_fmt","yuv420p"]
        args+=["-c:a","copy" if compatible else "aac","-b:a","128k",
               "-movflags","+faststart","-f","mp4",str(destination)]
        try:
            result=subprocess.run(args,capture_output=True,timeout=CONVERT_TIMEOUT,check=False,
                                  preexec_fn=_bound_child)
        except (OSError,subprocess.TimeoutExpired):
            raise MediaError("MEDIA_CONVERSION_UNAVAILABLE") from None
        if result.returncode or not destination.is_file():
            raise MediaError("MEDIA_CONVERSION_FAILED")
        after=self._probe(destination)
        if after["video"]!="h264" or any(x!="aac" for x in after["audio"]):
            raise MediaError("MEDIA_OUTPUT_NOT_MP4")
        with open(destination,"rb") as src:
            if src.read(32).find(b"ftyp")<0:
                raise MediaError("MEDIA_OUTPUT_NOT_MP4")
        size=destination.stat().st_size
        if size<=0 or size>MAX_OUTPUT_BYTES:
            raise MediaError("MEDIA_OUTPUT_SIZE_EXCEEDED")
        return size

class MediaSave:
    def __init__(self,discovery_store,root,downloader=None,transcoder=None,extractor=None):
        self.store=discovery_store
        self.root=Path(root)
        self.downloader=downloader or SafeDownloader()
        self.transcoder=transcoder or Transcoder()
        self.extractor=extractor or ExtractorFallback(self.root.parent / 'media-extractors' / 'bin')
        self.lock=threading.Lock()

    def _open(self):
        db=self.store._open()
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        columns={row[1] for row in db.execute("PRAGMA table_info(media_save_jobs)")}
        if "source_method" not in columns:
            db.execute("ALTER TABLE media_save_jobs ADD COLUMN source_method TEXT NOT NULL DEFAULT ''")
        return db

    def _directory(self):
        if (not self.root.is_dir() or self.root.is_symlink() or
            self.root.stat().st_uid!=os.getuid() or self.root.stat().st_mode & 0o077):
            raise MediaError("MEDIA_DIRECTORY_UNSAFE")
        return self.root

    def _space(self,bytes_needed=0):
        free=shutil.disk_usage(self._directory()).free
        if free<MIN_DISK_FREE_BYTES+bytes_needed:
            raise MediaError("MEDIA_DISK_FULL")

    def _candidate(self,db,candidate_id,index):
        if not isinstance(candidate_id,str) or not IDENT.fullmatch(candidate_id):
            raise MediaError("MEDIA_CANDIDATE_INVALID")
        if type(index) is not int or index<0:
            raise MediaError("MEDIA_INDEX_INVALID")
        row=db.execute("SELECT p.post_id,p.media_json,p.state,COALESCE(b.blocked,0),"
             "p.blog,p.url,p.post_type FROM discovery_posts p "
             "LEFT JOIN discovery_blogs b ON b.name=p.blog "
             "WHERE p.candidate_id=?",(candidate_id,)).fetchone()
        if not row or row[2]!="visible" or row[3]:
            raise MediaError("MEDIA_CANDIDATE_UNAVAILABLE")
        post_id,raw,_,_,blog,post_url,post_type=row
        try:
            verified_post_url(post_url,blog,post_id)
            media=json.loads(raw)
            if not isinstance(media,list):
                raise MediaError("MEDIA_SELECTION_INVALID")
            # A video post without any direct media may use its single
            # verified-post extractor fallback at index zero. Every other
            # index must exist in the *actual* stored list, not an invented cap.
            if index>=len(media) and (index!=0 or media):
                raise MediaError("MEDIA_INDEX_INVALID")
            selected=media[index] if index<len(media) else None
            if selected is not None and selected.get("type")=="video":
                accepted=_safe_media(selected)
                if accepted and accepted["type"]=="video":
                    _media_url(accepted["url"])
                    return post_id,accepted["url"],post_url,blog,True
            # A present but invalid video source is not evidence that the
            # provider has no direct source. Do not bypass its URL guard.
            if isinstance(selected,dict) and selected.get("type")=="video" and selected.get("url"):
                raise MediaError("MEDIA_SELECTION_INVALID")
            # Original Tumblr post with no direct video source, often video
            # blocks with a poster or externally embedded video metadata.
            # Only allow a single fallback target; never promote image posts.
            if index!=0 or post_type not in ("video","video/mp4"):
                raise MediaError("MEDIA_SELECTION_INVALID")
            # A verified video post may contain a video block with poster or
            # provider metadata but no usable direct source. Let the bounded
            # post extractor attempt that exact post; never select an image.
            videos=[i for i,item in enumerate(media)
                    if isinstance(item,dict) and item.get("type")=="video"]
            if videos and (videos!=[0] or not isinstance(selected,dict)
                           or selected.get("type")!="video"):
                raise MediaError("MEDIA_SELECTION_INVALID")
            return post_id,post_url,post_url,blog,False
        except MediaError:
            # Preserve exact selection/index/source errors. MediaError also
            # subclasses ValueError, so broad rewrapping previously hid them.
            raise
        except (ValueError,TypeError,KeyError,IndexError,AttributeError):
            raise MediaError("MEDIA_SELECTION_INVALID") from None

    def list(self,candidate_id=None):
        with closing(self._open()) as db:
            if candidate_id is None:
                rows=db.execute("SELECT id,candidate_id,source_post_id,media_index,state,"
                    "error_code,source_bytes,output_bytes,output_name,source_method,created_at,updated_at"
                    " FROM media_save_jobs ORDER BY created_at DESC LIMIT 100").fetchall()
            else:
                if not isinstance(candidate_id,str) or not IDENT.fullmatch(candidate_id):
                    raise MediaError("MEDIA_CANDIDATE_INVALID")
                rows=db.execute("SELECT id,candidate_id,source_post_id,media_index,state,"
                    "error_code,source_bytes,output_bytes,output_name,source_method,created_at,updated_at"
                    " FROM media_save_jobs WHERE candidate_id=? ORDER BY created_at DESC LIMIT 100",
                    (candidate_id,)).fetchall()
        keys=("id","candidate_id","source_post_id","media_index","state","error_code",
              "source_bytes","output_bytes","output_name","source_method","created_at","updated_at")
        return {"verified":True,"jobs":[dict(zip(keys,row)) for row in rows]}

    def file(self,job_id):
        if not isinstance(job_id,str) or not KEY.fullmatch(job_id):
            raise MediaError("MEDIA_JOB_INVALID")
        with closing(self._open()) as db:
            row=db.execute("SELECT state,output_name,output_bytes FROM media_save_jobs WHERE id=?",
                           (job_id,)).fetchone()
        if not row or row[0]!="completed" or row[1]!=job_id+".mp4":
            raise MediaError("MEDIA_JOB_NOT_READY")
        root=self._directory()
        path=root/row[1]
        if (path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077
                or path.stat().st_uid!=os.getuid() or path.stat().st_size!=row[2]):
            raise MediaError("MEDIA_FILE_UNSAFE")
        return path

    def save(self,candidate_id,media_index,rights_confirmed=False):
        if rights_confirmed is not True:
            raise MediaError("MEDIA_RIGHTS_CONFIRMATION_REQUIRED")
        if not self.lock.acquire(blocking=False):
            raise MediaError("MEDIA_BUSY")
        try:
            return self._save(candidate_id,media_index)
        finally:
            self.lock.release()

    def _save(self,candidate_id,media_index):
        self._directory()
        self._space()
        now=int(time.time())
        with closing(self._open()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                post_id,url,post_url,blog,has_direct=self._candidate(db,candidate_id,media_index)
                previous=db.execute("SELECT id,state,updated_at FROM media_save_jobs WHERE candidate_id=?"
                    " AND media_index=? AND source_url=?",(candidate_id,media_index,url)).fetchone()
                if previous and previous[1]=="completed":
                    match=db.execute("SELECT id,candidate_id,source_post_id,media_index,state,"
                        "error_code,source_bytes,output_bytes,output_name,source_method,created_at,updated_at"
                        " FROM media_save_jobs WHERE id=?",(previous[0],)).fetchone()
                    fields=("id","candidate_id","source_post_id","media_index","state","error_code",
                            "source_bytes","output_bytes","output_name","source_method","created_at","updated_at")
                    job=dict(zip(fields,match))
                    if job["output_name"] and (self.root / job["output_name"]).is_file():
                        db.execute("COMMIT")
                        return {"verified":True,"job":job,"from_cache":True}
                if previous and previous[1]=="running":
                    if now-previous[2]<600:
                        db.execute("ROLLBACK")
                        raise MediaError("MEDIA_JOB_IN_PROGRESS")
                    # Process died during a bounded job; only explicit owner retry
                    # may clear the interrupted state after the safety window.
                    db.execute("UPDATE media_save_jobs SET state='failed',"
                               "error_code='MEDIA_INTERRUPTED' WHERE id=?",(previous[0],))
                existing=db.execute("SELECT COALESCE(SUM(output_bytes),0) FROM media_save_jobs"
                                  " WHERE state='completed'").fetchone()[0]
                if existing+MAX_OUTPUT_BYTES>MAX_TOTAL_SAVED_BYTES:
                    db.execute("ROLLBACK")
                    raise MediaError("MEDIA_QUOTA_EXCEEDED")
                job_id=previous[0] if previous else uuid.uuid4().hex
                if previous:
                    db.execute("UPDATE media_save_jobs SET state='running',error_code='',updated_at=? "
                               "WHERE id=?",(now,job_id))
                else:
                    db.execute("INSERT INTO media_save_jobs(id,candidate_id,source_url,source_post_id,"
                               "media_index,state,created_at,updated_at) VALUES(?,?,?,?,?,'running',?,?)",
                               (job_id,candidate_id,url,post_id,media_index,now,now))
                db.execute("COMMIT")
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        source=self.root/(job_id+".source.part")
        partial=self.root/(job_id+".mp4.part")
        output=self.root/(job_id+".mp4")
        for path in (source,partial):
            if path.exists():
                path.unlink()
        try:
            # The public direct CDN source remains the first route. Only
            # known "not available" / unsupported cases advance to the post
            # extractors, never auth denials, 429, excess size or unsafe URLs.
            size=None
            method="direct"
            if has_direct:
                try:
                    size=self.downloader.probe(url)
                    if not isinstance(size,int) or size<1 or size>MAX_SOURCE_BYTES:
                        raise MediaError("MEDIA_SIZE_EXCEEDED")
                    self._space(size+MAX_OUTPUT_BYTES)
                    got=self.downloader.fetch(url,source,size)
                    if got!=size:
                        raise MediaError("MEDIA_SIZE_CHANGED")
                except MediaError as exc:
                    if str(exc) not in ("MEDIA_HTTP_UNAVAILABLE","MEDIA_LENGTH_UNKNOWN",
                                        "MEDIA_TYPE_UNSUPPORTED","MEDIA_HEAD_UNSUPPORTED"):
                        raise
                    if source.exists():
                        source.unlink()
                    size=None
            if size is None:
                # Reserve both bounded extractor attempts together, once for
                # this explicitly confirmed operation, across Gserver restarts.
                # Never bypass the read budget when provider capacity is gone.
                TumblrReadBudget(self.store.path.parent / "tumblr-api-budget.sqlite3").reserve(2)
                self._space(MAX_SOURCE_BYTES+MAX_OUTPUT_BYTES)
                try:
                    result=self.extractor.fetch(post_url,blog,post_id,source)
                except ExtractionFailure as exc:
                    raise MediaError(str(exc)) from None
                size=result["bytes"]
                method=result["method"]
                if not isinstance(size,int) or size<=0 or size>MAX_SOURCE_BYTES:
                    raise MediaError("MEDIA_SIZE_EXCEEDED")
            os.chmod(source,0o600)
            output_bytes=self.transcoder.convert(source,partial)
            os.chmod(partial,0o600)
            self._space(output_bytes)
            with open(partial,"rb") as digest_stream:
                digest=hashlib.file_digest(digest_stream,"sha256").hexdigest()
            os.replace(partial,output)
            with closing(self._open()) as db:
                db.execute("UPDATE media_save_jobs SET state='completed',source_bytes=?,"
                           "output_bytes=?,output_name=?,source_method=?,sha256=?,updated_at=? WHERE id=?",
                           (size,output_bytes,output.name,method,digest,int(time.time()),job_id))
            return {"verified":True,"job_id":job_id,"state":"completed",
                    "method":method,"source_bytes":size,"output_bytes":output_bytes,"output_name":output.name}
        except Exception as exc:
            error=str(exc) if isinstance(exc,MediaError) else "MEDIA_PROCESS_FAILED"
            try:
                with closing(self._open()) as db:
                    db.execute("UPDATE media_save_jobs SET state='failed',error_code=?,updated_at=? WHERE id=?",
                               (error,int(time.time()),job_id))
            except sqlite3.Error:
                pass
            if isinstance(exc,MediaError):
                raise
            raise MediaError("MEDIA_PROCESS_FAILED") from None
        finally:
            for path in (source,partial):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
