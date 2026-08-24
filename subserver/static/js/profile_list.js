/* Profile lists: client-side filtering, deletion, and folder creation. */
'use strict';

(function () {
  const filter = document.getElementById('profile-filter');
  if (filter) {
    filter.addEventListener('input', () => {
      const needle = filter.value.trim().toLowerCase();
      document.querySelectorAll('tr[data-search]').forEach((row) => {
        row.style.display = !needle || row.dataset.search.includes(needle) ? '' : 'none';
      });
    });
  }

  document.querySelectorAll('[data-delete]').forEach((button) => {
    button.addEventListener('click', async () => {
      const { scope, owner, folder, name } = button.dataset;
      if (!window.confirm(`Delete the profile "${name}"? This cannot be undone.`)) return;
      button.disabled = true;
      try {
        const result = await Sub.api('/api/profile/delete', { body: { scope, owner, folder, name } });
        Sub.toast(result.message, 'success');
        const row = button.closest('tr');
        if (row) row.remove();
      } catch (err) {
        Sub.toast(err.message, 'error');
        button.disabled = false;
      }
    });
  });

  const folderForm = document.getElementById('folder-form');
  if (folderForm) {
    folderForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const input = document.getElementById('new-folder');
      const scope = document.getElementById('folder-scope').value;
      const folder = input.value.trim();
      if (!folder) return;
      try {
        const result = await Sub.api('/api/folders', { body: { scope, folder } });
        Sub.toast(`Created ${scope}/${result.folder}`, 'success');
        input.value = '';
        setTimeout(() => window.location.reload(), 700);
      } catch (err) {
        Sub.toast(err.message, 'error');
      }
    });
  }
})();
