// Màn hình rà soát: xem trang PDF + phím tắt (J/K, Enter, Ctrl+S, A, N, Esc).
function reviewScreen(opts) {
  return {
    page: 1,
    pages: opts.pages,
    zoom: 1,
    sel: -1,
    dirty: false,
    anPham: opts.anPham,
    lanhDao: opts.lanhDao,
    thayThe: opts.thayThe,
    init() {
      this.rows = [...document.querySelectorAll('.frow[data-path]')];
      document.addEventListener('keydown', (e) => this.onKey(e));
      window.addEventListener('beforeunload', (e) => { if (this.dirty) e.preventDefault(); });
    },
    get src() { return `/documents/${opts.docId}/pages/${this.page}.png`; },
    go(p) { if (p >= 1 && p <= this.pages) this.page = p; },
    pick(i) {
      this.sel = i;
      const row = this.rows[i];
      if (!row) return;
      this.rows.forEach((r) => r.classList.remove('sel'));
      row.classList.add('sel');
      row.scrollIntoView({ block: 'nearest' });
      const p = parseInt(row.dataset.page || '0', 10);
      if (p) this.go(p);
    },
    focusRow(i) {
      this.pick(i);
      const input = this.rows[i]?.querySelector('input,textarea');
      if (input) input.focus();
    },
    onFocus(e) {
      const row = e.target.closest('.frow[data-path]');
      if (row) this.pick(this.rows.indexOf(row));
    },
    confirmRow(i) {
      const row = this.rows[i];
      if (!row) return;
      row.querySelector('input[type=hidden]').value = '1';
      row.classList.add('confirmed');
      row.classList.remove('c-medium', 'c-low');
      this.dirty = true;
    },
    submit(action) {
      this.dirty = false;
      const f = document.getElementById('review-form');
      f.querySelector('input[name=action]').value = action;
      f.requestSubmit ? f.requestSubmit() : f.submit();
    },
    onKey(e) {
      const tag = (e.target.tagName || '').toLowerCase();
      const typing = ['input', 'textarea', 'select'].includes(tag);
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); this.submit('save'); return; }
      if (typing) {
        if (e.key === 'Escape') { e.target.blur(); return; }
        if (e.key === 'Enter' && tag === 'input' && e.target.closest('.frow[data-path]')) {
          e.preventDefault(); this.confirmRow(this.sel); this.focusRow(this.sel + 1);
        }
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === 'j') { e.preventDefault(); this.focusRow(Math.min(this.sel + 1, this.rows.length - 1)); }
      else if (k === 'k') { e.preventDefault(); this.focusRow(Math.max(this.sel - 1, 0)); }
      else if (k === 'enter' && this.sel >= 0) { e.preventDefault(); this.confirmRow(this.sel); this.focusRow(this.sel + 1); }
      else if (k === 'a' && opts.canReview) { if (confirm('Duyệt cả document?')) this.submit('approve'); }
      else if (k === 'n') { if (!this.dirty || confirm('Có thay đổi chưa lưu. Bỏ qua và sang document tiếp?')) { this.dirty = false; location.href = `/review/next?after=${opts.docId}`; } }
      else if (k === 'arrowright') this.go(this.page + 1);
      else if (k === 'arrowleft') this.go(this.page - 1);
    },
  };
}
