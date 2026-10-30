// Runs first (external, CSP-safe): applies the saved Personal/Business mode before paint.
(function () {
  var mode = 'personal';
  try { mode = localStorage.getItem('vibeos_mode') === 'business' ? 'business' : 'personal'; } catch (e) {}
  // The old standalone business page now lives inside the main app.
  if (/\/business\.html$/.test(location.pathname)) {
    try { localStorage.setItem('vibeos_mode', 'business'); } catch (e) {}
    location.replace('/app/#business');
    return;
  }
  document.documentElement.setAttribute('data-mode', mode);
})();
