import React from 'react';
import {AbsoluteFill, Easing, interpolate, random, useCurrentFrame, useVideoConfig} from 'remotion';
import {BEAT, CLAMP, useSeconds} from './timing';

/** The blown-out frame at the moment of impact. */
export const Flash: React.FC = () => {
	const t = useSeconds();
	const o = interpolate(
		t,
		[BEAT.hit - 0.02, BEAT.hit + 0.02, BEAT.hit + 0.22],
		[0, 0.92, 0],
		CLAMP
	);
	if (o <= 0) {
		return null;
	}
	return <AbsoluteFill style={{backgroundColor: '#fff6e2', opacity: o}} />;
};

/** Expanding rings pushed out by the title landing. */
export const Shockwave: React.FC = () => {
	const t = useSeconds();

	return (
		<AbsoluteFill style={{mixBlendMode: 'screen'}}>
			{[0, 0.07, 0.15].map((delay, i) => {
				const p = interpolate(
					t,
					[BEAT.hit + delay, BEAT.hit + delay + 0.9],
					[0, 1],
					{...CLAMP, easing: Easing.out(Easing.cubic)}
				);
				if (p <= 0 || p >= 1) {
					return null;
				}
				const size = 80 + p * 2800;
				const opacity = (1 - p) * (0.9 - i * 0.24);
				const thickness = Math.max(1, (1 - p) * 16);
				return (
					<div
						key={i}
						style={{
							position: 'absolute',
							left: '50%',
							top: '50%',
							width: size,
							height: size,
							marginLeft: -size / 2,
							marginTop: -size / 2,
							borderRadius: '50%',
							border: `${thickness}px solid rgba(255,228,176,${opacity})`,
							filter: `blur(${2 + p * 12}px)`,
						}}
					/>
				);
			})}
		</AbsoluteFill>
	);
};

/** Anamorphic streak plus a couple of drifting flare discs. */
export const LensFlare: React.FC = () => {
	const t = useSeconds();

	const hitFlare = interpolate(
		t,
		[BEAT.hit - 0.03, BEAT.hit + 0.1, BEAT.hit + 1.3],
		[0, 1, 0.16],
		CLAMP
	);
	// a slow specular sweep across the frame once the title has settled
	const sweep = interpolate(t, [BEAT.hit + 0.7, BEAT.hit + 3.2], [-60, 160], CLAMP);
	const sweepOn = interpolate(
		t,
		[BEAT.hit + 0.7, BEAT.hit + 1.1, BEAT.hit + 2.8, BEAT.hit + 3.2],
		[0, 0.5, 0.5, 0],
		CLAMP
	);

	if (hitFlare <= 0 && sweepOn <= 0) {
		return null;
	}

	return (
		<AbsoluteFill style={{mixBlendMode: 'screen', pointerEvents: 'none'}}>
			{/* wide soft bar */}
			<div
				style={{
					position: 'absolute',
					top: '50%',
					left: '-15%',
					width: '130%',
					height: 46,
					marginTop: -23,
					opacity: hitFlare * 0.75,
					background:
						'linear-gradient(90deg, transparent 0%, rgba(120,180,255,0.35) 25%, rgba(210,230,255,0.85) 50%, rgba(120,180,255,0.35) 75%, transparent 100%)',
					filter: 'blur(28px)',
				}}
			/>
			{/* hot core line */}
			<div
				style={{
					position: 'absolute',
					top: '50%',
					left: '-10%',
					width: '120%',
					height: 5,
					marginTop: -2.5,
					opacity: hitFlare,
					background:
						'linear-gradient(90deg, transparent 0%, rgba(190,220,255,0.9) 30%, #ffffff 50%, rgba(190,220,255,0.9) 70%, transparent 100%)',
					filter: 'blur(4px)',
				}}
			/>
			{/* travelling specular */}
			<div
				style={{
					position: 'absolute',
					top: 0,
					bottom: 0,
					left: `${sweep}%`,
					width: '18%',
					opacity: sweepOn,
					background:
						'linear-gradient(90deg, transparent 0%, rgba(255,244,215,0.5) 50%, transparent 100%)',
					filter: 'blur(40px)',
					transform: 'skewX(-14deg)',
				}}
			/>
		</AbsoluteFill>
	);
};

// A tiny tile of fractal noise, inlined so nothing has to be fetched at render
// time. It is translated every frame, which is what makes it read as grain
// rather than as a texture.
const NOISE =
	"url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='220' height='220'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='220' height='220' filter='url(%23n)'/%3E%3C/svg%3E\")";

/** 35mm grain. Belongs to the print, so it sits outside the camera transform. */
export const Grain: React.FC = () => {
	const frame = useCurrentFrame();
	const dx = random(`gx-${frame}`) * 220;
	const dy = random(`gy-${frame}`) * 220;

	return (
		<AbsoluteFill
			style={{
				backgroundImage: NOISE,
				backgroundRepeat: 'repeat',
				backgroundPosition: `${dx}px ${dy}px`,
				opacity: 0.16,
				mixBlendMode: 'overlay',
				pointerEvents: 'none',
			}}
		/>
	);
};

/** Cinematic bars. They close in over the first beat and never leave. */
export const Letterbox: React.FC = () => {
	const t = useSeconds();
	const {height} = useVideoConfig();
	const bar = interpolate(t, [0, 1.1], [0, height * 0.107], {
		...CLAMP,
		easing: Easing.out(Easing.cubic),
	});

	return (
		<AbsoluteFill style={{pointerEvents: 'none'}}>
			<div style={{position: 'absolute', top: 0, left: 0, right: 0, height: bar, backgroundColor: '#000'}} />
			<div style={{position: 'absolute', bottom: 0, left: 0, right: 0, height: bar, backgroundColor: '#000'}} />
		</AbsoluteFill>
	);
};
