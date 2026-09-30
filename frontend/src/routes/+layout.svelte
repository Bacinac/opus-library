<script lang="ts">
	import '../app.css';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { Toasts, onUnauthorized } from '$lib/kit';
	import { Login, Shell, icons, me } from '$lib/opus';
	import { t } from '$lib/i18n';
	import { MODULE, MODULES } from '$lib/core/modules';

	let { children } = $props();

	// A link handed outside the household is a page of its own: whoever holds
	// it is not signed in, and the library around it is not theirs to see.
	const handed = $derived(page.url.pathname.startsWith('/s/'));

	onUnauthorized(() => (me.open = false));

	$effect(() => {
		if (!handed) me.check();
	});

	// A user's Library is the shelf, their own account and what the Library is.
	// A page whose every control the door refuses is worse than no page at all.
	const OTHERS_MAY = ['/', '/account', '/about'];
	const demo = import.meta.env.VITE_OPUS_DEMO === '1';
	$effect(() => {
		if (me.open && !me.admin && !handed && !OTHERS_MAY.includes(page.url.pathname)) goto('/');
	});

	// A user has one reason to be here and it is the photographs. Searching for a
	// record to acquire, watching what is downloading and editing the library's
	// settings are the work of whoever maintains it.
	const nav = $derived(
		me.admin
			? [
					{ href: '/', label: t('nav.library'), icon: icons.library },
					{ href: '/search', label: t('nav.search'), icon: icons.search },
					{ href: '/downloads', label: t('nav.downloads'), icon: icons.downloads },
					{ href: '/settings', label: t('nav.settings'), icon: icons.settings }
				]
			: [{ href: '/', label: t('nav.library'), icon: icons.library }]
	);
</script>

{#if handed}
	{@render children()}
	<Toasts />
{:else if me.open === false}
	<Login module={MODULE} onin={() => me.check()} />
	<Toasts />
{:else if me.open}
	<Shell
		module={MODULE}
		{nav}
		pathname={page.url.pathname}
		modules={MODULES}
		alerts={demo ? [{ key: 'demo', message: t('demo.banner'), tone: 'quiet' as const }] : []}
		owner={me.admin}
		account={me.name ? { username: me.name, href: '/account', onlogout: () => me.logout() } : undefined}
	>
		{@render children()}
	</Shell>
{:else}
	<Toasts />
{/if}

