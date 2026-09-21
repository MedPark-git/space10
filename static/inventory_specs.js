(() => {
  'use strict';
  const board = document.querySelector('.ivdash');
  if (!board) return;
  const groups = () => Array.from(board.querySelectorAll('[data-iv-product]'));

  document.getElementById('iv-expand')?.addEventListener('click', () => {
    groups().forEach(group => { group.open = true; });
  });
  document.getElementById('iv-collapse')?.addEventListener('click', () => {
    groups().forEach(group => { group.open = false; });
  });

  board.querySelectorAll('.iv-jump').forEach(link => {
    link.addEventListener('click', () => {
      const target = document.getElementById(link.hash.slice(1));
      if (target) target.open = true;
    });
  });

  const money = document.getElementById('iv-money');
  money?.addEventListener('change', () => {
    board.dataset.money = money.checked ? 'on' : 'off';
  });

  document.getElementById('iv-all-warehouses')?.addEventListener('click', () => {
    board.querySelectorAll('input[name="warehouse"]').forEach(input => { input.checked = false; });
    const summary = board.querySelector('.iv-warehouse > summary span');
    if (summary) summary.textContent = '전체';
  });

  board.querySelectorAll('input[name="warehouse"]').forEach(input => {
    input.addEventListener('change', () => {
      const count = board.querySelectorAll('input[name="warehouse"]:checked').length;
      const summary = board.querySelector('.iv-warehouse > summary span');
      if (summary) summary.textContent = count ? String(count) : '전체';
    });
  });
})();