import React from 'react';
import {AbsoluteFill, Easing, interpolate, random, useVideoConfig} from 'remotion';
import {BEAT, CLAMP, useSeconds} from './timing';

const DUST_COUNT = 120;
const EMBER_COUNT = 70;

/**
 * Backlit dust. It drifts through the tension beat, gets sucked toward the
 * centre while the piece charges, and is thrown outward by the hit.
 */
export const Dust: React.FC = () => {
	const t = useSeconds();
	const {width, height} = useVideoConfig();
	const cx = width / 2;
	const cy = height / 2;

	const appear = interpolate(t, [BEAT.raysIn, BEAT.raysIn + 1.4], [0, 1], CLAMP);
	const pull = interpolate(t, [BEAT.charge, BEAT.hit], [0, 0.84], {
		...CLAMP,
		easing: Easing.in(Easing.quad),
	});
	const burst = interpolate(t, [BEAT.hit, BEAT.hit + 1.1], [0, 1], {
		...CLAMP,
		easing: Easing.out(Easing.cubic),
	});

	// 1 = resting position, 0.16 = collapsed on the centre, >1 = thrown outward.
	const radial = 1 - pull + burst * 1.75;

	return (
		<AbsoluteFill style={{mixBlendMode: 'screen'}}>
			{new Array(DUST_COUNT).fill(0).map((_, i) => {
				const rx = random(`dx-${i}`);
				const ry = random(`dy-${i}`);
				const depth = random(`dz-${i}`); // 0 = far, 1 = near
				const phase = random(`dp-${i}`) * Math.PI * 2;
				const speed = 0.25 + random(`ds-${i}`) * 0.7;

				const baseX = rx * width;
				const baseY = ry * height;

				// gentle idle drift, stronger for near particles
				const driftX = Math.sin(t * speed + phase) * (10 + depth * 26);
				const driftY = -t * (6 + depth * 20) + Math.cos(t * speed * 0.8 + phase) * (8 + depth * 18);

				const x = cx + (baseX - cx) * radial + driftX;
				const y = cy + (baseY - cy) * radial + driftY;

				const size = 1.4 + depth * 3.4;
				const opacity =
					appear * (0.12 + depth * 0.5) * (0.55 + 0.45 * Math.sin(t * 1.6 + phase)) * (1 - burst * 0.45);

				return (
					<div
						key={i}
						style={{
							position: 'absolute',
							left: x,
							top: y,
							width: size,
							height: size,
							borderRadius: '50%',
							backgroundColor: 'rgba(255,236,205,1)',
							opacity: Math.max(0, opacity),
							filter: `blur(${(1 - depth) * 2.2}px)`,
							boxShadow: `0 0 ${4 + depth * 10}px rgba(255,206,140,0.7)`,
						}}
					/>
				);
			})}
		</AbsoluteFill>
	);
};

/**
 * Warm embers rising after the hit. Each one loops on its own lifetime, so the
 * field never empties and never needs a spawner.
 */
export const Embers: React.FC = () => {
	const t = useSeconds();
	const {width, height} = useVideoConfig();

	const intensity = interpolate(
		t,
		[BEAT.hit - 0.1, BEAT.hit + 0.5, BEAT.fade],
		[0, 1, 0.85],
		CLAMP
	);
	if (intensity <= 0) {
		return null;
	}

	const LIFE = 2.8;

	return (
		<AbsoluteFill style={{mixBlendMode: 'screen'}}>
			{new Array(EMBER_COUNT).fill(0).map((_, i) => {
				const rx = random(`ex-${i}`);
				const rise = 0.45 + random(`er-${i}`) * 0.75;
				const phase = random(`ep-${i}`);
				const wobble = random(`ew-${i}`) * Math.PI * 2;
				const size = 2 + random(`es-${i}`) * 4.5;

				const age = ((t * (0.55 + rise * 0.4)) / LIFE + phase) % 1;
				const y = height * 1.04 - age * height * rise * 1.15;
				const x = rx * width + Math.sin(age * 7 + wobble) * (18 + rx * 40);

				// fade in and out over the ember's own life
				const life = Math.sin(age * Math.PI);
				const opacity = life * life * intensity * (0.35 + random(`eo-${i}`) * 0.6);

				return (
					<div
						key={i}
						style={{
							position: 'absolute',
							left: x,
							top: y,
							width: size,
							height: size,
							borderRadius: '50%',
							backgroundColor: '#ffd08a',
							opacity,
							filter: 'blur(0.6px)',
							boxShadow: `0 0 ${8 + size * 3}px rgba(255,150,40,0.95), 0 0 ${
								18 + size * 6
							}px rgba(255,90,10,0.5)`,
						}}
					/>
				);
			})}
		</AbsoluteFill>
	);
};
