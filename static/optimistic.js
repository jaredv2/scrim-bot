// Optimistic UI for dashboard — fast feedback for kills/points/wins
// Intercepts .opt-* buttons, updates DOM instantly, syncs via fetch, rolls back on failure.
// No external deps besides jQuery already loaded. Uses fetch + X-Requested-With.
(function() {
  const EVT = window.EVENT_ID || (typeof EVENT_ID !== 'undefined' ? EVENT_ID : null);
  if (!EVT) return;

  const toast = (msg, ok = true) => {
    let c = document.getElementById('opt-toast');
    if (!c) {
      c = document.createElement('div');
      c.id = 'opt-toast';
      c.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;';
      document.body.appendChild(c);
    }
    const el = document.createElement('div');
    el.textContent = msg;
    el.style.cssText = `padding:10px 14px;border-radius:6px;font-size:13px;color:#fff;box-shadow:0 4px 12px rgba(0,0,0,.2);background:${ok ? '#27ae60' : '#e74c3c'};transition:opacity .3s`;
    c.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 2500);
  };

  const parseIntSafe = (v) => { const n = parseInt(v, 10); return isNaN(n) ? 0 : n; };

  // Find the row for a discord_id in the leaderboard (solo or team). Returns {rowEl, killsCell, pointsCell}
  function findRow(discordId) {
    // Leaderboard rows have data-discord attribute (added below) or contain the ID in text.
    let row = document.querySelector(`tr[data-discord="${discordId}"]`);
    if (row) return row;
    // Fallback: search text
    const rows = document.querySelectorAll('#tab-leaderboard tbody tr, #tab-players tbody tr');
    for (const r of rows) {
      if (r.textContent.includes(discordId)) return r;
    }
    return null;
  }

  function optimisticIncrement(discordId, kind, delta) {
    const row = findRow(discordId);
    if (!row) return { revert: () => {} };
    // Heuristic: kills is col 3 (index 3), points col 2 for solo. For team, similar.
    // We store previous values in dataset for revert.
    const cells = row.querySelectorAll('td');
    let targetCell = null;
    let prevVal = 0;
    if (kind === 'kill') {
      // kills cell is last or second last depending on team vs solo — search by header
      // Solo: # | Player | Points | Kills  -> kills is index 3
      // Team: # | Players | Points | Kills -> kills is index 3
      targetCell = cells[3] || cells[cells.length - 1];
    } else if (kind === 'win' || kind === 'points') {
      targetCell = cells[2] || cells[1];
    }
    if (!targetCell) return { revert: () => {} };
    prevVal = parseIntSafe(targetCell.textContent);
    const nextVal = prevVal + delta;
    // Save
    targetCell.dataset.prev = String(prevVal);
    targetCell.textContent = String(nextVal);
    targetCell.classList.add('opt-pending');
    targetCell.style.transition = 'background .3s';
    targetCell.style.background = delta > 0 ? '#d5f5e3' : '#fadbd8';
    // Also add a small badge
    row.classList.add('opt-row-pending');
    return {
      revert: () => {
        targetCell.textContent = String(prevVal);
        targetCell.classList.remove('opt-pending');
        targetCell.style.background = '';
        row.classList.remove('opt-row-pending');
      },
      commit: () => {
        targetCell.classList.remove('opt-pending');
        targetCell.style.background = '#d5f5e3';
        setTimeout(() => targetCell.style.background = '', 800);
        row.classList.remove('opt-row-pending');
      }
    };
  }

  // Generic helper: POST with optimistic update
  async function postOptimistic(url, data, opts) {
    const { discord_id, kind, delta, successMsg } = opts;
    const handle = discord_id ? optimisticIncrement(discord_id, kind, delta) : { revert: () => {}, commit: () => {} };
    // Disable the button that triggered
    const btn = opts.btn;
    const prevDisabled = btn ? btn.disabled : false;
    const prevText = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.classList.add('opt-btn-pending'); }

    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams(data).toString(),
      });
      const json = await res.json();
      if (!res.ok || json.error) throw new Error(json.error || `HTTP ${res.status}`);
      handle.commit();
      if (successMsg) toast(successMsg, true);
      // Optionally update with server truth if returned
      if (json.points_added !== undefined && kind === 'kill' && btn) {
        // server confirms points_added, already reflected
      }
      return json;
    } catch (e) {
      handle.revert();
      toast(e.message || 'Failed — reverted', false);
      throw e;
    } finally {
      if (btn) { btn.disabled = prevDisabled; btn.textContent = prevText; btn.classList.remove('opt-btn-pending'); }
    }
  }

  // Hook up quick-kill / quick-win buttons (added in template as .opt-kill etc.)
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('.opt-kill, .opt-win, .opt-dq, .opt-undq, .opt-elim, .opt-add-point, .opt-remove-point');
    if (!btn) return;
    e.preventDefault();
    const discordId = btn.dataset.discord;
    const gameNumber = btn.dataset.game || '1';
    if (!discordId) return;

    let url, data, kind, delta, msg;
    if (btn.classList.contains('opt-kill')) {
      url = `/event/${EVT}/ajax/quick-kill`;
      data = { discord_id: discordId, game_number: gameNumber };
      kind = 'kill'; delta = 1; msg = `+1 kill for ${discordId}`;
    } else if (btn.classList.contains('opt-win')) {
      url = `/event/${EVT}/ajax/quick-win`;
      data = { discord_id: discordId, game_number: gameNumber };
      kind = 'win'; delta = 1; msg = `+1 win for ${discordId}`;
    } else if (btn.classList.contains('opt-add-point')) {
      const pts = parseInt(btn.dataset.points || '1', 10);
      url = `/event/${EVT}/ajax/update-points`;
      // This endpoint expects point_kill/point_win/placement_scale — we reuse quick-kill's points logic
      // For generic point add, use quick-kill with custom points? Fallback to quick-kill
      url = `/event/${EVT}/ajax/quick-kill`;
      data = { discord_id: discordId, game_number: gameNumber };
      kind = 'points'; delta = pts; msg = `+${pts} pts`;
    } else if (btn.classList.contains('opt-remove-point')) {
      url = `/event/${EVT}/ajax/remove-points`;
      data = { discord_id: discordId, points: btn.dataset.points || '1' };
      kind = 'points'; delta = -parseInt(btn.dataset.points || '1', 10); msg = `-${data.points} pts`;
    } else if (btn.classList.contains('opt-dq')) {
      url = `/event/${EVT}/ajax/dq-player`;
      data = { discord_id: discordId, reason: btn.dataset.reason || 'No reason' };
      kind = null; delta = 0; msg = `DQ ${discordId}`;
      // Optimistic: strike-through row
      const row = findRow(discordId);
      if (row) { row.style.opacity = '0.5'; row.style.textDecoration = 'line-through'; }
    } else if (btn.classList.contains('opt-elim')) {
      url = `/event/${EVT}/ajax/eliminate`;
      data = { discord_id: discordId, game_number: gameNumber };
      kind = null; delta = 0; msg = `Eliminated ${discordId}`;
    }
    if (!url) return;
    try {
      await postOptimistic(url, data, { discord_id: discordId, kind, delta, successMsg: msg, btn });
      // For eliminate/dq, reload placement badges after success
      if (btn.classList.contains('opt-elim') || btn.classList.contains('opt-dq')) {
        // Fetch fresh placement-status to update badges without full reload
        const r = await fetch(`/event/${EVT}/ajax/placement-status?game_number=${gameNumber}`, { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
        const j = await r.json();
        if (j.projected) {
          // Update each player's projected placement badge if present
          j.projected.forEach(p => {
            const badge = document.querySelector(`[data-badge="${p.discord_id}"]`);
            if (badge) badge.textContent = p.placement ? `#${p.placement}` : `~#${p.projected}`;
          });
        }
      }
    } catch {}
  });

  // Kill feed form: optimistic dispatch
  const killForm = document.getElementById('kill-feed-form');
  if (killForm) {
    killForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(killForm);
      const killer = fd.get('killer_id');
      const victim = fd.get('victim_id');
      if (!killer || !victim) return;
      const btn = killForm.querySelector('button[type=\"submit\"]');
      const origText = btn ? btn.textContent : '';
      if (btn) { btn.disabled = true; btn.textContent = 'Dispatching…'; }
      // Optimistic: show in a temporary feed
      let feed = document.getElementById('opt-kill-feed');
      if (!feed) {
        feed = document.createElement('div');
        feed.id = 'opt-kill-feed';
        feed.style.cssText = 'margin-top:8px;font-size:12px;color:#666;';
        killForm.after(feed);
      }
      const entry = document.createElement('div');
      entry.textContent = `💀 ${killer} → ${victim} (pending…)`;
      entry.style.opacity = '0.6';
      feed.prepend(entry);
      try {
        await postOptimistic(`/event/${EVT}/ajax/dispatch-kill`, { killer_id: killer, victim_id: victim, weapon: fd.get('weapon') || '' }, { kind: null, delta: 0, successMsg: `Kill ${killer} → ${victim}`, btn });
        entry.textContent = `💀 ${killer} → ${victim} ✓`;
        entry.style.opacity = '1';
        entry.style.color = '#27ae60';
        killForm.reset();
      } catch (err) {
        entry.textContent = `💀 ${killer} → ${victim} ✗ ${err.message || ''}`;
        entry.style.color = '#e74c3c';
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = origText; }
        setTimeout(() => entry.remove(), 4000);
      }
    }, { capture: true });
    // Prevent double handler (original jQuery handler still there) — remove it by cloning
    // The original handler is jQuery; our fetch handler runs first and we already preventedDefault, so jQuery's handler is also prevented by our early return? Actually we called preventDefault, so jQuery's handler (also preventDefault) will still run but its $.post will double-send. To avoid double, we stopPropagation and remove jQuery handler.
    // Clone form to drop jQuery listeners and re-add only ours (we already added ours)
    // Instead, just off jQuery's handler if present
    if (window.$) {
      try { window.$('#kill-feed-form').off('submit'); } catch {}
    }
  }

  // Placement scale form: optimistic save with inline feedback (already has, but enhance)
  const scaleForm = document.getElementById('placement-scale-form');
  if (scaleForm) {
    scaleForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const input = document.getElementById('placement-scale');
      const status = document.getElementById('scale-status');
      const val = input.value.trim();
      const prev = input.dataset.prev || val;
      input.dataset.prev = val;
      status.textContent = 'Saving…'; status.style.color = '#3498db';
      try {
        const res = await fetch(`/event/${EVT}/ajax/update-points`, {
          method: 'POST',
          headers: { 'X-Requested-With': 'XMLHttpRequest', 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({ placement_scale: val }).toString(),
        });
        const j = await res.json();
        if (!res.ok) throw new Error(j.error || 'Failed');
        status.textContent = 'Saved ✓'; status.style.color = '#27ae60';
        toast('Placement scale saved', true);
      } catch (err) {
        input.value = prev;
        status.textContent = err.message || 'Error saving'; status.style.color = '#e74c3c';
        toast('Save failed — reverted', false);
      }
      setTimeout(() => status.textContent = '', 2500);
    });
    if (window.$) { try { window.$('#placement-scale-form').off('submit'); } catch {} }
  }

  console.log('[optimistic] ready for event', EVT);
})();
