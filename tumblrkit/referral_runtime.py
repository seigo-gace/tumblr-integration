"""Durable, private Referral owner logic backed by the existing campaign ledger.

Click events, confirmed GATE memberships, and rewards remain separate facts.
No public join URLs or GATE callbacks are invented by this module.
"""
import hashlib
import json
import time
from tumblrkit.core import issue_referral, confirm_membership

SCHEMA="""
CREATE TABLE IF NOT EXISTS referral_revocations(
 campaign_id TEXT NOT NULL,
 referrer_id TEXT NOT NULL,
 revoked_at INTEGER NOT NULL,
 PRIMARY KEY(campaign_id,referrer_id),
 FOREIGN KEY(campaign_id,referrer_id)
 REFERENCES referrals(campaign_id,referrer_id)
);
"""

class ReferralManager:
    def __init__(self, db, *, gate=None, clock=None):
        self.db=db
        self.gate=gate
        self.clock=clock or time.time
        db.execute("PRAGMA foreign_keys=ON")
        with db:
            db.executescript(SCHEMA)

    @staticmethod
    def _ident(value):
        if (not isinstance(value,str) or not 1<=len(value)<=100
            or any(ord(c)<33 for c in value)):
            raise ValueError("referral_identifier_invalid")
        return value

    def set_tier(self,campaign_id,threshold,reward):
        """Configure the existing reward-tier table before referrals begin.

        Terms are frozen after the first referral code or verified member.
        This is a ledger mutation only; it never sends or grants a prize.
        """
        self._ident(campaign_id)
        if (type(threshold) is not int or not 1<=threshold<=10000 or
                not isinstance(reward,str) or not 1<=len(reward.strip())<=150):
            raise ValueError("referral_tier_invalid")
        with self.db:
            campaign=self.db.execute(
                "SELECT active FROM campaigns WHERE id=?",
                (campaign_id,)).fetchone()
            if not campaign or campaign[0]!=1:
                raise ValueError("referral_campaign_missing")
            touched=self.db.execute(
                "SELECT (SELECT COUNT(*) FROM referrals WHERE campaign_id=?) + "
                "(SELECT COUNT(*) FROM memberships WHERE campaign_id=?) + "
                "(SELECT COUNT(*) FROM grants WHERE campaign_id=?)",
                (campaign_id,campaign_id,campaign_id)).fetchone()[0]
            if touched:
                raise ValueError("referral_tiers_locked")
            self.db.execute(
                "INSERT INTO tiers(campaign_id,threshold,reward) VALUES(?,?,?) "
                "ON CONFLICT(campaign_id,threshold) DO UPDATE SET reward=excluded.reward",
                (campaign_id,threshold,reward.strip()))
        return {"campaign_id":campaign_id,"threshold":threshold,
                "reward":reward.strip(),"state":"configured",
                "grants_issued":False}

    def issue(self,campaign_id,referrer_id):
        self._ident(campaign_id)
        self._ident(referrer_id)
        row=self.db.execute(
            "SELECT revoked_at FROM referral_revocations "
            "WHERE campaign_id=? AND referrer_id=?",
            (campaign_id,referrer_id)).fetchone()
        if row:
            raise ValueError("referral_revoked")
        code=issue_referral(self.db,campaign_id,referrer_id)
        # An opaque internal code is not a public invitation URL.
        return {"campaign_id":campaign_id,"referrer_id":referrer_id,
                "referral_code":code,"status":"active"}

    def revoke(self,campaign_id,referrer_id):
        self._ident(campaign_id)
        self._ident(referrer_id)
        with self.db:
            exists=self.db.execute(
                "SELECT 1 FROM referrals WHERE campaign_id=? AND referrer_id=?",
                (campaign_id,referrer_id)).fetchone()
            if not exists:
                raise ValueError("referral_not_found")
            created=self.db.execute(
                "INSERT OR IGNORE INTO referral_revocations VALUES(?,?,?)",
                (campaign_id,referrer_id,int(self.clock()))).rowcount
        return "revoked" if created else "already_revoked"

    def confirm(self,campaign_id,member_id,referral_code=None):
        """Only trusted GATE adapter can confirm a real member and grant rewards."""
        self._ident(campaign_id)
        self._ident(member_id)
        if self.gate is None:
            raise PermissionError("referral_gate_not_connected")
        if self.gate(campaign_id,member_id) is not True:
            raise PermissionError("referral_gate_not_verified")
        if referral_code is not None:
            if not isinstance(referral_code,str) or not referral_code:
                raise ValueError("referral_code_invalid")
            row=self.db.execute(
                "SELECT r.referrer_id FROM referrals r "
                "LEFT JOIN referral_revocations v ON v.campaign_id=r.campaign_id "
                "AND v.referrer_id=r.referrer_id "
                "WHERE r.campaign_id=? AND r.code=? AND v.referrer_id IS NULL",
                (campaign_id,referral_code)).fetchone()
            if not row:
                raise ValueError("referral_code_inactive")
            if row[0]==member_id:
                raise ValueError("referral_self_entry")
        grants=confirm_membership(
            self.db,campaign_id,member_id,gate_verified=True,
            referral_code=referral_code)
        return {"state":"verified","new_rewards":grants}

    def report(self,campaign_id):
        """Observed click/verified member/claimed tier counts only, no estimates."""
        self._ident(campaign_id)
        exists=self.db.execute("SELECT 1 FROM campaigns WHERE id=?",
                               (campaign_id,)).fetchone()
        if not exists:
            raise ValueError("referral_campaign_missing")
        rows=self.db.execute(
            "SELECT r.referrer_id, r.code, (v.revoked_at IS NOT NULL),"
            "(SELECT COUNT(*) FROM memberships m WHERE m.campaign_id=r.campaign_id "
            "AND m.referrer_id=r.referrer_id),"
            "(SELECT COUNT(*) FROM grants g WHERE g.campaign_id=r.campaign_id "
            "AND g.referrer_id=r.referrer_id) "
            "FROM referrals r LEFT JOIN referral_revocations v "
            "ON v.campaign_id=r.campaign_id AND v.referrer_id=r.referrer_id "
            "WHERE r.campaign_id=? ORDER BY r.referrer_id",(campaign_id,)).fetchall()
        tiers=self.db.execute(
            "SELECT threshold,reward FROM tiers WHERE campaign_id=? ORDER BY threshold",
            (campaign_id,)).fetchall()
        granted=self.db.execute(
            "SELECT referrer_id,threshold,awarded_at FROM grants WHERE campaign_id=?",
            (campaign_id,)).fetchall()
        granted_at={(person,threshold):at for person,threshold,at in granted}
        return {"campaign_id":campaign_id,"referrers":[
            {"referrer_id":r[0],"referral_code":r[1],
             "status":"revoked" if r[2] else "active",
             "verified_members":r[3],"awarded_tiers":r[4],
             "tier_progress":[
                 {"threshold":threshold,"reward":reward,
                  "verified_members":r[3],
                  "remaining":max(0,threshold-r[3]),
                  "awarded":(r[0],threshold) in granted_at,
                  "awarded_at":granted_at.get((r[0],threshold))}
                 for threshold,reward in tiers]
            } for r in rows],
            "basis":"owner_private_gate_verified_ledger",
            "clicks_are_not_memberships":True}
