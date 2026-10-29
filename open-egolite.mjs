const task = await taskSpace(`Z62 integrated simulator ${Date.now()}`);
await task.page('p1').goto(globalThis.z62ConsoleUrl || 'http://127.0.0.1:8642');
await task.finish({keep:['p1']});
