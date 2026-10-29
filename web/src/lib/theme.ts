export type ThemePref = "light" | "dark" | "system";

const KEY = "stoxsy.theme";

export function resolve(pref: ThemePref, systemDark: boolean): "light" | "dark" {
  return pref === "system" ? (systemDark ? "dark" : "light") : pref;
}

export function readPref(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

export function applyPref(pref: ThemePref) {
  const dark = typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  document.documentElement.setAttribute("data-theme", resolve(pref, !!dark));
  document.documentElement.setAttribute("data-theme-pref", pref);
  try {
    if (pref === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, pref);
  } catch {
    /* storage blocked: the choice simply does not persist */
  }
}

export function nextPref(p: ThemePref): ThemePref {
  return p === "system" ? "light" : p === "light" ? "dark" : "system";
}
