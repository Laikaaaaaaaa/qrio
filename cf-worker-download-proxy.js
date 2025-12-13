/**
 * Cloudflare Worker: download proxy
 *
 * Usage:
 *   https://<worker-host>/?u=<encoded_upstream_url>&name=<encoded_filename>
 *
 * It streams the upstream file while forcing download and setting a stable
 * filename via Content-Disposition.
 */

function encodeRFC5987ValueChars(str) {
	return encodeURIComponent(str)
		.replace(/['()]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`)
		.replace(/\*/g, '%2A');
}

export default {
	async fetch(request) {
		const requestUrl = new URL(request.url);
		const upstream = requestUrl.searchParams.get('u');
		const name = requestUrl.searchParams.get('name') || 'download';

		if (!upstream) {
			return new Response('Missing query param: u', { status: 400 });
		}

		let upstreamUrl;
		try {
			upstreamUrl = new URL(upstream);
		} catch {
			return new Response('Invalid upstream URL', { status: 400 });
		}

		if (!['http:', 'https:'].includes(upstreamUrl.protocol)) {
			return new Response('Upstream must be http/https', { status: 400 });
		}

		const upstreamResp = await fetch(upstreamUrl.toString(), {
			redirect: 'follow',
		});

		if (!upstreamResp.ok || !upstreamResp.body) {
			return new Response(`Upstream error: ${upstreamResp.status}`, { status: 502 });
		}

		const headers = new Headers(upstreamResp.headers);
		headers.set('Content-Disposition', `attachment; filename*=UTF-8''${encodeRFC5987ValueChars(name)}`);
		headers.set('Cache-Control', 'no-store');

		return new Response(upstreamResp.body, {
			status: upstreamResp.status,
			headers,
		});
	},
};
