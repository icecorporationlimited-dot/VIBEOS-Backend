// Asset Studio API: upload / list / delete asset files straight into the
// VIBEOS-Backend repo (assets/{category}/{folder}/...) via the GitHub Contents API.
//
// Required env vars (Netlify > Site settings > Environment variables):
//   ASSET_GITHUB_TOKEN   fine-grained PAT: Contents Read+Write, ONLY this repo
//   ASSET_UPLOAD_PASSCODE  a secret passcode the uploader page must send
//   ASSET_REPO (optional) "owner/repo", defaults to the VIBEOS backend

const FOLDERS = {
  vibe: ["fonts", "stickers", "frames", "textures"],
  collage: ["fonts", "papers", "scraps", "stickers", "tapes"],
};
const EXT = {
  fonts: ["ttf", "otf"],
  stickers: ["png"],
  frames: ["png"],
  textures: ["png", "jpg", "jpeg"],
  papers: ["jpg", "jpeg", "png"],
  scraps: ["png"],
  tapes: ["png"],
};
const MAX_BYTES = 16 * 1024 * 1024;
const REPO = process.env.ASSET_REPO || "icecorporationlimited-dot/VIBEOS-Backend";
const BRANCH = process.env.ASSET_BRANCH || "main";

const json = (status, body) =>
  Response.json(body, { status, headers: { "Cache-Control": "no-store" } });

function authed(req) {
  const pc = process.env.ASSET_UPLOAD_PASSCODE;
  if (!pc) return { ok: true };                        // no passcode configured = open
  return { ok: (req.headers.get("x-upload-key") || "") === pc };
}

function snake(name) {
  return String(name).toLowerCase().replace(/[^a-z0-9._-]+/g, "_")
    .replace(/_+/g, "_").replace(/^_|_$/g, "").slice(0, 80);
}

function ghPath(category, folder, file) {
  return `assets/${category}/${folder}/${encodeURIComponent(file)}?ref=${BRANCH}`;
}

async function gh(path, init = {}) {
  return fetch(`https://api.github.com/repos/${REPO}/contents/${path}`, {
    ...init,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${process.env.ASSET_GITHUB_TOKEN}`,
      "User-Agent": "vibeos-asset-studio",
      ...(init.headers || {}),
    },
  });
}

async function updateManifest(category, folder, name, remove = false) {
  try {
    const mPath = `assets/${category}/manifest.json?ref=${BRANCH}`;
    let sha, manifest = {};
    const cur = await gh(mPath);
    if (cur.ok) {
      const j = await cur.json();
      sha = j.sha;
      manifest = JSON.parse(Buffer.from(j.content, "base64").toString("utf8"));
    }
    const list = new Set(manifest[folder] || []);
    if (remove) list.delete(name); else list.add(name);
    manifest[folder] = Array.from(list).sort();
    await gh(mPath, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: `assets: ${remove ? "remove" : "add"} ${category}/${folder}/${name} [manifest]`,
        content: Buffer.from(JSON.stringify(manifest, null, 2)).toString("base64"),
        branch: BRANCH,
        ...(sha ? { sha } : {}),
      }),
    });
  } catch (e) {
    // manifest is a convenience only; never fail the upload for it
  }
}

export default async (req) => {
  if (!process.env.ASSET_GITHUB_TOKEN) {
    return json(500, { error: "ASSET_GITHUB_TOKEN is not configured on Netlify" });
  }
  const a = authed(req);
  if (!a.ok) return json(401, { error: "wrong passcode" });
  const url = new URL(req.url);

  try {
    // ---------- list ----------
    if (req.method === "GET") {
      const category = url.searchParams.get("category");
      const folder = url.searchParams.get("folder");
      if (!FOLDERS[category] || !FOLDERS[category].includes(folder)) {
        return json(400, { error: "unknown category/folder" });
      }
      const res = await gh(ghPath(category, folder, ""));
      if (res.status === 404) return json(200, { files: [] });
      if (!res.ok) return json(502, { error: "github list failed" });
      const data = await res.json();
      return json(200, { files: (Array.isArray(data) ? data : [])
        .filter((f) => f.type === "file").map((f) => ({ name: f.name, size: f.size, sha: f.sha })) });
    }

    // ---------- delete ----------
    if (req.method === "DELETE") {
      const { category, folder, name } = await req.json();
      const file = snake(name);
      if (!FOLDERS[category] || !FOLDERS[category].includes(folder)) {
        return json(400, { error: "unknown category/folder" });
      }
      const info = await gh(ghPath(category, folder, file));
      if (!info.ok) return json(404, { error: "file not found" });
      const meta = await info.json();
      const res = await gh(ghPath(category, folder, file), {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: `assets: remove ${category}/${folder}/${file}`, sha: meta.sha, branch: BRANCH }),
      });
      if (!res.ok) return json(502, { error: "github delete failed" });
      await updateManifest(category, folder, file, true);
      return json(200, { ok: true, deleted: file });
    }

    // ---------- upload ----------
    if (req.method === "POST") {
      const body = await req.json();
      const { category, folder, ext = "", overwrite = false } = body;
      const name = snake(body.name);
      if (!name) return json(400, { error: "file name is required" });
      if (!FOLDERS[category] || !FOLDERS[category].includes(folder)) {
        return json(400, { error: "unknown category/folder" });
      }
      const allowed = EXT[folder] || [];
      if (!allowed.includes(String(ext).toLowerCase())) {
        return json(400, { error: `.${ext} not allowed in ${folder} (allowed: ${allowed.join(", ")})` });
      }
      const data = String(body.data || "");
      const bytes = Math.floor((data.length * 3) / 4);
      if (!data || bytes < 50) return json(400, { error: "file looks empty" });
      if (bytes > MAX_BYTES) return json(413, { error: "file larger than 16 MB" });

      const file = `${name}.${String(ext).toLowerCase()}`;
      const put = (sha) => gh(ghPath(category, folder, file), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: `assets: add ${category}/${folder}/${file} [asset studio]`,
          content: data.replace(/^data:[^,]+,/, ""),
          branch: BRANCH,
          ...(sha ? { sha } : {}),
        }),
      });

      let res = await put(null);
      if (res.status === 422) {                          // already exists: update when told to
        if (!overwrite) return json(409, { error: `${file} already exists`, exists: true });
        const cur = await gh(ghPath(category, folder, file));
        if (!cur.ok) return json(502, { error: "github read failed" });
        const meta = await cur.json();
        res = await put(meta.sha);
      }
      if (!res.ok) return json(502, { error: "github upload failed" });
      const out = await res.json();
      await updateManifest(category, folder, file);
      return json(200, { ok: true, path: out.content?.path || `assets/${category}/${folder}/${file}`,
        url: out.content?.html_url || "", commit: out.commit?.sha || "" });
    }

    return json(405, { error: "method not allowed" });
  } catch (e) {
    return json(500, { error: "upload failed: " + (e && e.message) });
  }
};
