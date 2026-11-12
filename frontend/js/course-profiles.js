/* Course presets are shared with the server; custom entries remain lecturer-owned. */
(function () {
  "use strict";
  const select = document.getElementById("course-profile");
  const language = document.getElementById("source-language");
  const note = document.getElementById("course-profile-note");
  const preview = document.getElementById("course-preview");
  const custom = document.getElementById("course-glossary");
  let courses = [];
  function refresh() {
    const supported = language.value === "id";
    select.disabled = !supported || !courses.length;
    if (!supported) select.value = "";
    const course = courses.find(c => c.id === select.value);
    preview.textContent = course ? course.terms.map(([s, t]) => s + " = " + t).join("\n") : "No preset selected.";
    note.textContent = !supported ? "IT presets currently support Indonesian input. Your custom terms are kept."
      : course ? course.terms.length + " starter terms. Review with your lecturer; additional terms below override matching entries."
      : "Choose a course to apply its starter terms, or enter a custom glossary below.";
  }
  BLB.courseProfiles = {
    settings() {
      const pairs = new Map();
      const course = courses.find(c => c.id === select.value);
      if (course) course.terms.forEach(([s, t]) => pairs.set(s.toLowerCase(), t));
      const seen = new Set();
      if (custom.value.length > 8000) throw new Error("Glossary must be at most 8000 characters.");
      for (const line of custom.value.split(/\r?\n/).filter(l => l.trim())) {
        const at = line.indexOf("="), s = line.slice(0, at).trim(), t = line.slice(at + 1).trim();
        if (at < 0 || !s || !t || Math.max(s.length, t.length) > 120)
          throw new Error("Use source term = English term, max 120 characters per term.");
        const key = s.toLowerCase();
        if (seen.has(key)) throw new Error("Duplicate glossary source term.");
        seen.add(key); pairs.set(key, t);
      }
      if (pairs.size > 40) throw new Error("At most 40 terms including the course preset. Remove extra entries or choose Custom glossary only.");
      return {course_profile: select.value, glossary: custom.value};
    }
  };
  select.addEventListener("change", refresh);
  language.addEventListener("change", refresh);
  let initialized = false;
  BLB.courseProfiles.init = function () {
  if (initialized) return;
  initialized = true;
  fetch("data/it-courses.json").then(r => {
    if (!r.ok) throw new Error("Catalog unavailable");
    return r.json();
  }).then(data => {
    if (!Array.isArray(data.courses)) throw new Error("Invalid catalog");
    courses = data.courses;
    courses.forEach(c => {
      const option = document.createElement("option");
      option.value = c.id; option.textContent = c.label; select.appendChild(option);
    });
    refresh();
  }).catch(() => {
    select.disabled = true;
    note.textContent = "Course presets could not load. Custom glossary is still available. Reload to retry.";
  });
  };
})();
