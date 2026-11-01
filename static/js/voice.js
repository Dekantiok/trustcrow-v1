/* Shared voice-note helper: record with MediaRecorder, POST the blob,
   hand the transcript to a callback. Used by the AI chat and manual forms.
   No audio ever leaves the device except to our own transcription endpoint.
*/
(function () {
  'use strict';

  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (m) return decodeURIComponent(m[1]);
    var el = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return el ? el.value : '';
  }

  function supported() {
    return !!(window.MediaRecorder && navigator.mediaDevices &&
              navigator.mediaDevices.getUserMedia);
  }

  function recordOnce(onDone, onError) {
    if (!supported()) {
      onError(new Error('Voice recording is not supported in this browser.'));
      return function () {};
    }
    var recorder = null;
    var chunks = [];
    var stopped = false;
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      if (stopped) {
        stream.getTracks().forEach(function (t) { t.stop(); });
        return;
      }
      var mime = '';
      if (window.MediaRecorder.isTypeSupported) {
        if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) mime = 'audio/webm;codecs=opus';
        else if (MediaRecorder.isTypeSupported('audio/webm')) mime = 'audio/webm';
        else if (MediaRecorder.isTypeSupported('audio/mp4')) mime = 'audio/mp4';
      }
      try {
        recorder = mime ? new MediaRecorder(stream, { mimeType: mime })
                        : new MediaRecorder(stream);
      } catch (e) {
        stream.getTracks().forEach(function (t) { t.stop(); });
        onError(e);
        return;
      }
      recorder.ondataavailable = function (ev) {
        if (ev.data && ev.data.size) chunks.push(ev.data);
      };
      recorder.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        onDone(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }));
      };
      recorder.start();
      recordOnce.active = recorder;
    }).catch(onError);
    return function stop() {
      stopped = true;
      var r = recordOnce.active;
      if (r && r.state !== 'inactive') r.stop();
      recordOnce.active = null;
    };
  }
  recordOnce.active = null;
  recordOnce.supported = supported;

  function postAudio(url, blob, onOk, onErr) {
    var fd = new FormData();
    fd.append('audio', blob, 'note.webm');
    fetch(url, {
      method: 'POST',
      headers: { 'X-CSRFToken': csrfToken() },
      body: fd,
    }).then(function (resp) { return resp.json().then(function (data) {
      return { status: resp.status, data: data };
    }); }).then(function (out) {
      if (out.status >= 200 && out.status < 300 && out.data && out.data.ok !== false) onOk(out.data);
      else onErr(new Error((out.data && (out.data.error || out.data.transcript)) || 'Transcription failed.'));
    }).catch(onErr);
  }

  /* Mic toggle button for text inputs/textareas with data-voice. */
  function attachFieldMic(field) {
    if (field.dataset.voiceAttached) return;
    field.dataset.voiceAttached = '1';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-sm btn-outline-dark-custom voice-mic-btn';
    btn.title = 'Record a voice note to fill this field';
    btn.setAttribute('aria-label', 'Record voice note for ' + (field.name || 'field'));
    btn.innerHTML = '<i class="bi bi-mic"></i>';
    btn.style.marginTop = '6px';
    var stop = null;
    var hint = document.createElement('small');
    hint.className = 'text-muted-custom d-block mt-1 voice-hint';
    btn.addEventListener('click', function () {
      if (stop) {
        stop(); stop = null;
        btn.innerHTML = '<i class="bi bi-hourglass-split"></i>';
        btn.disabled = true;
        return;
      }
      btn.innerHTML = '<i class="bi bi-stop-circle"></i> Recording… tap to stop';
      hint.textContent = '';
      stop = recordOnce(function (blob) {
        stop = null;
        postAudio(field.dataset.voiceUrl || '/assistant/transcribe/', blob, function (data) {
          if (field.tagName === 'TEXTAREA' && field.value && data.transcript) field.value += ' ';
          field.value += data.transcript || '';
          field.dispatchEvent(new Event('input', { bubbles: true }));
          btn.innerHTML = '<i class="bi bi-mic"></i>';
          btn.disabled = false;
          hint.textContent = '';
        }, function (err) {
          hint.textContent = (err && err.message) || 'Voice failed — please type.';
          btn.innerHTML = '<i class="bi bi-mic"></i>';
          btn.disabled = false;
        });
      }, function () {
        stop = null;
        btn.innerHTML = '<i class="bi bi-mic"></i>';
        hint.textContent = 'Microphone unavailable.';
      });
    });
    field.insertAdjacentElement('afterend', btn);
    btn.insertAdjacentElement('afterend', hint);
  }

  function initFieldMics() {
    document.querySelectorAll('input[data-voice], textarea[data-voice]').forEach(attachFieldMic);
  }

  function initChatMic() {
    var mic = document.getElementById('chat-mic');
    var hint = document.getElementById('chat-voice-hint');
    var result = document.getElementById('chat-result');
    if (!mic || !result) return;
    if (!supported()) { mic.style.display = 'none'; return; }
    if (mic.dataset.voiceAttached) return;
    mic.dataset.voiceAttached = '1';
    var stop = null;
    function setIdle() {
      stop = null;
      mic.innerHTML = '<i class="bi bi-mic"></i>';
      mic.disabled = false;
    }
    mic.addEventListener('click', function () {
      if (stop) {
        mic.disabled = true;
        mic.innerHTML = '<i class="bi bi-hourglass-split"></i>';
        if (hint) hint.textContent = 'Transcribing…';
        stop();
        stop = null;
        return;
      }
      if (hint) hint.textContent = '';
      mic.innerHTML = '<i class="bi bi-stop-circle"></i>';
      stop = recordOnce(function (blob) {
        var fd = new FormData();
        fd.append('audio', blob, 'note.webm');
        fetch('/assistant/voice/', {
          method: 'POST',
          headers: { 'X-CSRFToken': csrfToken() },
          body: fd,
        }).then(function (resp) { return resp.text(); }).then(function (html) {
          result.innerHTML = html;
          if (typeof scrollThread === 'function') scrollThread();
          else {
            var thread = document.getElementById('chat-thread');
            if (thread) thread.scrollTop = thread.scrollHeight;
          }
          if (hint) hint.textContent = '';
          setIdle();
        }).catch(function () {
          if (hint) hint.textContent = 'Voice failed — please type instead.';
          setIdle();
        });
      }, function () {
        if (hint) hint.textContent = 'Microphone unavailable.';
        setIdle();
      });
    });
  }

  window.TrustcrowVoice = {
    supported: supported,
    recordOnce: recordOnce,
    postAudio: postAudio,
    initFieldMics: initFieldMics,
    initChatMic: initChatMic,
  };

  document.addEventListener('DOMContentLoaded', initFieldMics);
})();
