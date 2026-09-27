// 定制 hook：微信 4.x RadiumWMPF 25715
// OnLoadStart: 强制 is_main_frame=1, 写入调试门槛 (+0x2d4=1) 与调试 scene (+0x1c8=1101)
// CDP filter: 解除消息过滤

const getMainModule = () => Process.findModuleByName("flue.dll");

let sceneForceDone = false;
let cdpPatched = false;

const patchOnLoadStart25715 = (base) => {
  Interceptor.attach(base.add(0x2D450F0), {
    onEnter() {
      if ((this.context.rdx & 0xff) !== 1) {
        this.context.rdx = (this.context.rdx & ~0xff) | 0x1;
      }
      if (sceneForceDone) return;
      try {
        const rsi = this.context.rcx;
        const p1 = rsi.add(0x40).readPointer();
        const p2 = p1.add(0x5e0).readPointer();
        const p3 = p2.add(0x10).readPointer();
        const gate = p3.add(0x2d4).readInt();
        const scene = p3.add(0x1c8).readInt();
        const flag = p3.add(0x1b7).readS8();
        const ptr1a8 = p3.add(0x1a8).readPointer();
        send(`[hook] gate(+0x2d4)=${gate} scene(+0x1c8)=${scene} flag(+0x1b7)=${flag} ptr(+0x1a8)=${ptr1a8}`);
        if (gate !== 1) p3.add(0x2d4).writeInt(1);
        if (scene !== 1101) p3.add(0x1c8).writeInt(1101);
        // flag 必须为正且非零, 否则调试会话调用被跳过
        p3.add(0x1b7).writeS8(1);
        // 第二条链: [[[this+0x40]+0x620]+8]+0x5e0]+0x10 (调试会话函数检查的对象)
        try {
            const rax = rsi.add(0x40).readPointer();
            const arg = rax.add(0x620).readPointer();
            const q1 = arg.add(8).readPointer();
            const q2 = q1.add(0x5e0).readPointer();
            const obj = q2.add(0x10).readPointer();
            const g2 = obj.add(0x2d4).readInt();
            const s2 = obj.add(0x1c8).readInt();
            send(`[hook] chain2 gate=${g2} scene=${s2}`);
            obj.add(0x2d4).writeInt(1);
            obj.add(0x1c8).writeInt(1101);
            obj.add(0x1b7).writeS8(1);
            send("[hook] chain2 forced");
        } catch (e) {
            send(`[hook] chain2 err: ${e}`);
        }
        sceneForceDone = true;
        send("[hook] debug gate + scene + flag forced (1101)");
      } catch (e) {
        send(`[hook] scene patch err: ${e}`);
      }
    },
    onLeave() {},
  });
};

const patchCDPFilter = (base) => {
  Interceptor.attach(base.add(0x39F5310), {
    onEnter(args) { this.inputValue = args[0]; },
    onLeave(retval) {
      try {
        const p = this.inputValue.readPointer();
        if (p.isNull()) return;
        const f = p.add(8);
        if (f.readU32() === 6) {
          f.writeU32(0x0);
          if (!cdpPatched) {
            send("[patch] CDP filter 6 -> 0");
            cdpPatched = true;
          }
        }
      } catch (e) {}
    },
  });
};

const main = () => {
  const mainModule = Process.findModuleByName("flue.dll");
  if (!mainModule) {
    send("[frida] flue.dll not found in this process");
    return;
  }
  send(`[frida] flue.dll base = ${mainModule.base}`);
  patchOnLoadStart25715(mainModule.base);
  patchCDPFilter(mainModule.base);
  send("[frida-ready] hooks installed (25715)");
};

main();
