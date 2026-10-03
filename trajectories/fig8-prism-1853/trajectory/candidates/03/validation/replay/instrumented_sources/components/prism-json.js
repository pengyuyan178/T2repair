;globalThis["__cg_c53f301c8f2aaa7f"]&&globalThis["__cg_c53f301c8f2aaa7f"].loaded("components/prism-json.js");;globalThis["__cg_c53f301c8f2aaa7f"]&&globalThis["__cg_c53f301c8f2aaa7f"].hit("H8",()=>undefined);;globalThis["__cg_c53f301c8f2aaa7f"]&&globalThis["__cg_c53f301c8f2aaa7f"].hit("H7",()=>undefined);;globalThis["__cg_c53f301c8f2aaa7f"]&&globalThis["__cg_c53f301c8f2aaa7f"].hit("H6",()=>undefined);;globalThis["__cg_c53f301c8f2aaa7f"]&&globalThis["__cg_c53f301c8f2aaa7f"].hit("H5",()=>undefined);;globalThis["__cg_c53f301c8f2aaa7f"]&&globalThis["__cg_c53f301c8f2aaa7f"].hit("H4",()=>undefined);;globalThis["__cg_c53f301c8f2aaa7f"]&&globalThis["__cg_c53f301c8f2aaa7f"].hit("H3",()=>undefined);;globalThis["__cg_c53f301c8f2aaa7f"]&&globalThis["__cg_c53f301c8f2aaa7f"].hit("H2",()=>undefined);;globalThis["__cg_c53f301c8f2aaa7f"]&&globalThis["__cg_c53f301c8f2aaa7f"].hit("H1",()=>undefined);Prism.languages.json = {
	'property': {
		pattern: /(^|[^\\])"(?:\\.|[^\\"\r\n])*"(?=\s*:)/,
		lookbehind: true,
		greedy: true
	},
	'string': {
		pattern: /(^|[^\\])"(?:\\.|[^\\"\r\n])*"(?!\s*:)/,
		lookbehind: true,
		greedy: true
	},
	'comment': {
		pattern: /(^|[^\\])\/\/.*|\/\*[\s\S]*?(?:\*\/|$)/,
		lookbehind: true
	},
	'number': /-?\d+\.?\d*(e[+-]?\d+)?/i,
	'punctuation': /[{}[\],]/,
	'operator': /:/,
	'boolean': /\b(?:true|false)\b/,
	'null': {
		pattern: /\bnull\b/,
		alias: 'keyword'
	}
};
