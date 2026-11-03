"use strict";
const assert = require("node:assert/strict");
global.BLB = {};
const sources = [];
class Context {
  constructor() { this.currentTime=0; this.state="running"; this.destination={}; }
  async resume() { this.state="running"; }
  async suspend() { this.state="suspended"; }
  createBuffer(channels, length, rate) {
    const data=new Float32Array(length);
    return {duration:length/rate,getChannelData:()=>data};
  }
  createBufferSource() {
    const node={connect(){},disconnect(){},start(t){this.when=t;},stop(){this.stopped=true;}};
    sources.push(node);return node;
  }
}
global.AudioContext=Context;
const PCMPlayer=require("../js/pcm-player.js");
(async()=>{
 let starts=0,overflows=0;
 const p=new PCMPlayer({onStart:()=>starts++, onBacklog:()=>overflows++});
 await p.resume();
 const data=Buffer.from([0,0,255,127,0,128]).toString("base64");
 p.push(data,0);p.push(data,0);
 assert.equal(sources[0].buffer.getChannelData(0)[2],-1);
 assert.ok(sources[1].when >= sources[0].when+sources[0].buffer.duration);
 p.context.currentTime=.05;
 await new Promise(r=>setTimeout(r,30));
 assert.equal(starts,1,"one first-playback report per segment");
 await p.pause();assert.equal(p.context.state,"suspended");
 await p.resume();assert.equal(p.context.state,"running");
 p.next=10;p.push(data,1);assert.equal(overflows,1);
 assert.equal(p.nodes.size,0);assert.equal(p.timers.size,0);
 assert.ok(sources.every(n=>n.stopped));
 console.log("PCM player: signed samples, ordered scheduling, one playback report, pause/resume and bounded backlog passed.");
})().catch(e=>{console.error(e);process.exitCode=1;});
