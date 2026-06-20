package com.reasonix.app

import android.content.Intent
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.widget.ArrayAdapter
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.isVisible
import androidx.lifecycle.lifecycleScope
import com.reasonix.app.databinding.ActivitySetupBinding
import kotlinx.coroutines.launch

/**
 * First-launch screen: collects the user's API key and provider choice,
 * writes the config, then starts the main activity.
 */
class SetupActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySetupBinding

    // Supported provider presets
    private data class ProviderPreset(
        val name: String,
        val kind: String,
        val defaultModel: String,
        val keyPrefix: String = ""
    )

    private val providers = listOf(
        ProviderPreset("DeepSeek", "deepseek", "deepseek-v4-pro"),
        ProviderPreset("Anthropic", "anthropic", "claude-sonnet-4-20250514", "sk-ant-"),
        ProviderPreset("OpenAI", "openai", "gpt-4o", "sk-"),
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySetupBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setupProviderDropdown()
        setupListeners()
    }

    private fun setupProviderDropdown() {
        val names = providers.map { it.name }
        val adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item, names)
        binding.providerSpinner.setAdapter(adapter)
        binding.providerSpinner.setText(names[0], false)
    }

    private fun setupListeners() {
        var modelListener: TextWatcher? = null

        binding.providerSpinner.setOnItemClickListener { _, _, position, _ ->
            val preset = providers[position]
            binding.modelInput.removeTextChangedListener(modelListener)

            // Auto-fill model when provider changes
            binding.modelInput.setText(preset.defaultModel)
            if (preset.keyPrefix.isNotEmpty()) {
                binding.keyInput.hint = "${preset.keyPrefix}…"
            } else {
                binding.keyInput.hint = getString(R.string.setup_key_hint)
            }

            // Re-sync model to the preset when provider changes
            modelListener = object : TextWatcher {
                override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
                override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
                override fun afterTextChanged(s: Editable?) {}
            }
            binding.modelInput.addTextChangedListener(modelListener)
        }

        binding.startButton.setOnClickListener { onStartClicked() }
        binding.skipButton.setOnClickListener { onSkipClicked() }
    }

    private fun onStartClicked() {
        val providerName = binding.providerSpinner.text.toString()
        val preset = providers.find { it.name == providerName } ?: return
        val model = binding.modelInput.text.toString().trim()
        val apiKey = binding.keyInput.text.toString().trim()

        if (apiKey.isBlank()) {
            Toast.makeText(this, R.string.setup_error, Toast.LENGTH_SHORT).show()
            return
        }

        binding.startButton.isEnabled = false
        binding.startButton.text = "Starting…"
        binding.progressBar.isVisible = true

        lifecycleScope.launch {
            // Save credentials
            ConfigHelper.saveCredentials(this@SetupActivity, preset.kind, model, apiKey)
            // Mark as launched so we skip setup next time
            ConfigHelper.markLaunched(this@SetupActivity)

            startMainActivity()
        }
    }

    private fun onSkipClicked() {
        // Allow skipping — user can configure later through the web UI
        ConfigHelper.markLaunched(this)
        startMainActivity()
    }

    private fun startMainActivity() {
        val intent = Intent(this, MainActivity::class.java)
        intent.flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
        startActivity(intent)
        finish()
    }
}
